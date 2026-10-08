// Persistent AI operations wrap the original fetch result; existing production callbacks still run.
(() => {
  const originalFetch = window.fetch.bind(window);
  const owned = new Set();
  const routes = new Set(['/api/generate_script','/api/rewrite_script','/api/parse_script_text',
    '/api/expand_script','/api/asset_prompt','/api/story_prompt','/api/asset_gen','/api/boogu_gen']);
  let active = null, polling = false, workflowResumed = false;
  const el = id => document.getElementById(id);
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const json = (path, body) => originalFetch(path, body === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  window.fetch = async function(path, options) {
    if (routes.has(path) && !(await saveProjectNow(true))) throw new Error('项目保存失败，请重试；AI 请求尚未发送。');
    const response = await originalFetch(path, options);
    if (!routes.has(path) || response.status !== 202) return response;
    const first = await response.json();
    if (!first.ai_job) return new Response(JSON.stringify(first), {status:response.status});
    const jid = first.ai_job;
    owned.add(jid);
    try {
      for (;;) {
        await wait(500);
        let job;
        try {
          const r = await json('/api/ai/job?id=' + encodeURIComponent(jid));
          if (!r.ok) throw new Error('AI 操作记录读取失败');
          job = await r.json();
        } catch (e) {
          el('aiConnection').textContent = '控制台暂未连接；请求已保存，重启后会继续。';
          continue;
        }
        if (job.status !== 'done') continue;
        await json('/api/ai/ack', {job:jid});
        if (job.project !== currentProjectName) {
          throw new Error('结果已保存到原项目「' + job.project + '」，请打开该项目查看。');
        }
        return new Response(JSON.stringify(job.result), {status:job.code,
          headers:{'Content-Type':'application/json'}});
      }
    } finally { owned.delete(jid); }
  };
  async function refresh() {
    if (polling) return;
    polling = true;
    try {
      const response = await json('/api/ai/work');
      if (!response.ok) return;
      const work = await response.json();
      const pending = work.pending || [];
      el('aiConnection').textContent = '';
      el('aiManualPanel').hidden = !pending.length;
      const select = el('aiRequestSelect');
      const signature = JSON.stringify(pending.map(p => p.id));
      if (select.dataset.signature !== signature) {
        select.replaceChildren(...pending.map(p => new Option(
          p.project + ' · ' + p.step + (p.purpose ? ' · ' + p.purpose : '') + ' · ' + (p.kind === 'image_gen' ? '图片' : '文本'), p.id)));
        select.dataset.signature = signature;
        if (active && pending.some(p => p.id === active.id)) select.value = active.id;
      }
      const next = pending.find(p => p.id === select.value);
      if (next && (!active || next.id !== active.id)) {
        active = next;
        el('aiRequestText').value = next.text;
        el('aiResultText').value = '';
        el('aiResultImage').value = '';
        el('aiTextReturn').hidden = next.kind === 'image_gen';
        el('aiImageReturn').hidden = next.kind !== 'image_gen';
        el('aiReferences').replaceChildren();
        el('aiReferences').textContent = next.kind === 'image_gen'
          ? '本次原接口请求未携带参考图（保持原有文生图调用）。' : '';
        for (const [i,url] of (next.references || []).entries()) {
          const a = document.createElement('a'); a.href = url; a.download = '参考图' + (i+1) + '.png';
          const img = document.createElement('img'); img.src = url; img.style.maxWidth = '160px';
          a.append(img); el('aiReferences').append(a);
        }
        el('aiSubmit').textContent = next.kind === 'image_gen' ? '确认图片并继续' : '提交结果并继续';
        el('aiManualError').textContent = '';
      }
      for (const job of work.jobs || []) {
        if (job.status !== 'done' || owned.has(job.id)) continue;
        if (job.project === currentProjectName) {
          await loadProject();
          if (job.code === 200) {
            const step = job.path.includes('expand_script') ? 4 : job.path.includes('rewrite_script') ? 3
              : job.path.includes('script') ? 2 : 5;
            goStep(step);
            if (step === 5 && currentScript) renderAssets();
            if (job.path.includes('rewrite_script') && scriptAfter) {
              renderScriptCompare(scriptBefore, 'beforeScript'); renderScriptCompare(scriptAfter, 'afterScript');
              el('btnConfirmScript').disabled = false;
            }
          }
        }
        toast(job.code === 200 ? job.step + '已完成，结果已保存到「' + job.project + '」。'
          : job.step + '失败：' + (job.result.error || '未知错误'), job.code !== 200);
        if (job.project === currentProjectName && job.code === 200 && job.context.next === 'image'
            && !localStorage.getItem('aiAssetWorkflow:' + currentProjectName)) {
          rememberAssetWorkflow(job.context.kind, job.context.idx, job.context.key);
        }
        await json('/api/ai/ack', {job:job.id});

      }
      if (!workflowResumed && !window.aiAssetWorkflowActive && !owned.size && !pending.length && !(work.jobs || []).some(j => j.status === 'running')) {
        const saved = localStorage.getItem('aiAssetWorkflow:' + currentProjectName);
        if (saved && currentScript) {
          workflowResumed = true;
          await loadProject(); renderAssets();
          const intent = JSON.parse(saved);
          if (intent.type === 'image') {
            const image = (assetImgs[intent.kind] || {})[intent.key] || '';
            if (image && image !== intent.before) finishAssetWorkflow();
            else if (intent.kind === 'role') genRoleImg(intent.idx);
            else if (intent.kind === 'scene') genSceneImg(intent.idx);
            else genStoryImg(intent.idx);
          } else if (intent.type === 'views') genRoleFourViews(intent.i);
          else genAllAssets();
        }
      }
    } catch (e) { /* A stopped console leaves journal intact. */ }
    finally { polling = false; }
  }
  document.addEventListener('DOMContentLoaded', () => {
    el('aiRequestSelect').onchange = refresh;
    el('aiCopy').onclick = async () => {
      try { await navigator.clipboard.writeText(el('aiRequestText').value); }
      catch (e) { el('aiRequestText').select(); document.execCommand('copy'); }
      el('aiCopy').textContent = '已复制'; setTimeout(() => el('aiCopy').textContent = '一键复制', 1500);
    };
    el('aiSubmit').onclick = async () => {
      if (!active) return;
      const request = active;
      el('aiSubmit').disabled = true;
      try {
        let image = '';
        if (request.kind === 'image_gen') {
          const file = el('aiResultImage').files[0];
          if (!file) throw new Error('请上传生成图片');
          if (file.size > 16 * 1024 * 1024) throw new Error('图片不能超过 16MB');
          image = await new Promise((resolve, reject) => {
            const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]);
            reader.onerror = reject; reader.readAsDataURL(file);
          });
        }
        const r = await json('/api/ai/submit', {id:request.id, job:request.job,
          text:el('aiResultText').value, image});
        const d = await r.json(); if (!r.ok) throw new Error(d.error);
        active = null;
        toast('结果已提交，正在继续原有步骤。');
        await refresh();
      } catch (e) { el('aiManualError').textContent = e.message; }
      finally { el('aiSubmit').disabled = false; }
    };
    setInterval(refresh, 1000); setTimeout(refresh, 1500);
  });
})();
