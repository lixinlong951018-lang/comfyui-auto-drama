#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Semantic MiniMax H3 workflow adapter for ComfyUI API graphs.

The console deals in t2va/i2va/fl2va/ref2va tasks. Concrete ComfyUI node ids
stay inside API workflow JSON. Current FL2VA/I2VA baseline: pruned INT8
ConvRot, no Turbo LoRA, no H3 MemEff Sage patch, SageAttn3 (compile off),
AV shift 12/3, res_multistep + simple, 8 steps, INT8 ConvRot video VAE.
"""
from __future__ import annotations
import copy, json, os
from typing import Any

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)

ALIASES = {
    "t2v":"t2va","t2va":"t2va","i2v":"i2va","i2va":"i2va",
    "fl2v":"fl2va","fl2va":"fl2va","r2v":"ref2va",
    "ref2v":"ref2va","ref2va":"ref2va",
}
COMMON = {
    "clip":"qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "video_vae":"minimax_h3_video_vae_int8_convrot.safetensors",
    "audio_vae":"minimax_h3_audio_vae_fp32.safetensors",
    "sampler":"res_multistep","scheduler":"simple",
    "sage_attention":"sageattn3","allow_compile":False,
    "shift_video":12.0,"shift_audio":3.0,
}
PROFILES = {
    "t2va": {**COMMON, "unet":"minimax_h3_fl2va_pruned_int8_convrot.safetensors",
              "steps":20, "allow_task_steps":False},
    "i2va": {**COMMON, "unet":"minimax_h3_fl2va_pruned_int8_convrot.safetensors",
              "steps":8, "allow_task_steps":True},
    "fl2va":{**COMMON, "unet":"minimax_h3_fl2va_pruned_int8_convrot.safetensors",
              "steps":8, "allow_task_steps":True},
    "ref2va":{**COMMON, "unet":"minimax_h3_ref2va_pruned_int8_convrot.safetensors",
               "steps":20, "allow_task_steps":False},
}
FORBIDDEN = {"MiniMaxH3TurboLoRA","MiniMaxH3MemoryEfficientSageAttentionPatch","EasyCache"}

def _config():
    path = os.environ.get("BATCH_CONSOLE_CONFIG") or os.path.join(ROOT, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def resolve_mode(task):
    raw = str(task.get("mode") or "").lower().strip()
    mode = ALIASES.get(raw)
    if mode is None:
        if task.get("images"): mode = "ref2va"
        elif task.get("image") and task.get("last_frame"): mode = "fl2va"
        elif task.get("image"): mode = "i2va"
        else: mode = "t2va"
    if mode == "i2va" and task.get("last_frame"):
        mode = "fl2va"
    return mode

def _profile(mode, task):
    p = copy.deepcopy(PROFILES[mode])
    override = (((_config().get("h3") or {}).get("profiles") or {}).get(mode) or {})
    if isinstance(override, dict):
        p.update({k:v for k,v in override.items() if v is not None})
    if p.get("allow_task_steps") and task.get("steps") is not None:
        try: p["steps"] = max(1, int(task["steps"]))
        except (TypeError, ValueError): pass
    if mode == "ref2va":
        if task.get("r2v_unet"): p["unet"] = task["r2v_unet"]
        if task.get("r2v_clip"): p["clip"] = task["r2v_clip"]
    return p

def _api_graph(data):
    if isinstance(data, dict) and data and all(
        isinstance(v, dict) and "class_type" in v for v in data.values()
    ):
        return copy.deepcopy(data)
    if isinstance(data, dict) and isinstance(data.get("prompt"), dict):
        return copy.deepcopy(data["prompt"])
    try:
        g = data["queue_running"][0][2]
        if isinstance(g, dict): return copy.deepcopy(g)
    except Exception:
        pass
    raise ValueError("H3 自定义工作流必须导出为 ComfyUI API/prompt JSON")

def load_custom_workflow(mode):
    mode = ALIASES.get(str(mode).lower(), str(mode).lower())
    item = (((_config().get("h3") or {}).get("workflows") or {}).get(mode))
    path = str(item.get("path") if isinstance(item, dict) else (item or "")).strip()
    if not path: return None
    if not os.path.isabs(path): path = os.path.join(ROOT, path)
    if not os.path.isfile(path): raise FileNotFoundError("H3 API workflow 不存在: " + path)
    with open(path, encoding="utf-8") as f: return _api_graph(json.load(f))

def _nodes(g, cls):
    return [(str(i), n) for i,n in g.items()
            if isinstance(n, dict) and n.get("class_type") == cls]

def _new_id(g):
    nums = [int(str(k)) for k in g if str(k).isdigit()]
    return str(max(nums or [900000]) + 1)

def _link(v):
    return isinstance(v, list) and len(v) >= 2 and isinstance(v[0], (str,int))

def _rewire(g, old, new, exclude=()):
    skip = {str(x) for x in exclude}
    for nid,node in g.items():
        if str(nid) in skip or not isinstance(node, dict): continue
        for key,val in list((node.get("inputs") or {}).items()):
            if _link(val) and str(val[0]) == str(old):
                node["inputs"][key] = list(new)

def _bypass(g, nid):
    node = g.get(nid)
    up = (node.get("inputs") or {}).get("model") if isinstance(node, dict) else None
    if not _link(up): return False
    _rewire(g, nid, up)
    g.pop(nid, None)
    return True

def _strip_accel(g):
    for _ in range(5):
        changed = False
        for nid,node in list(g.items()):
            if isinstance(node, dict) and node.get("class_type") in FORBIDDEN:
                changed = _bypass(g, str(nid)) or changed
        if not changed: break
    for cls in ("PathchSageAttentionKJ","MiniMaxH3SigmaShift"):
        for nid,_ in list(_nodes(g, cls)): _bypass(g, nid)

def _model_chain(g, p):
    rows = _nodes(g, "UNETLoader")
    if not rows: raise ValueError("H3 workflow 缺少 UNETLoader")
    uid,unet = rows[0]
    unet.setdefault("inputs", {})["unet_name"] = p["unet"]
    sid = _new_id(g)
    g[sid] = {"class_type":"PathchSageAttentionKJ","inputs":{
        "model":[uid,0],"sage_attention":p["sage_attention"],
        "allow_compile":bool(p["allow_compile"])}}
    xid = _new_id(g)
    g[xid] = {"class_type":"MiniMaxH3SigmaShift","inputs":{
        "model":[sid,0],"shift_video":float(p["shift_video"]),
        "shift_audio":float(p["shift_audio"])}}
    _rewire(g, uid, [xid,0], exclude=(sid,xid))

def _common(g, task, p):
    for _,n in _nodes(g,"CLIPLoader"): n.setdefault("inputs",{})["clip_name"] = p["clip"]
    for _,n in _nodes(g,"VAELoader"):
        inp=n.setdefault("inputs",{}); cur=str(inp.get("vae_name") or "").lower()
        inp["vae_name"] = p["audio_vae"] if "audio" in cur else p["video_vae"]
    for _,n in _nodes(g,"BasicScheduler"):
        inp=n.setdefault("inputs",{}); inp["scheduler"]=p["scheduler"]; inp["steps"]=int(p["steps"])
    samplers=_nodes(g,"KSamplerSelect")
    if samplers:
        kid,kn=samplers[0]; kn.setdefault("inputs",{})["sampler_name"]=p["sampler"]
    else:
        kid=_new_id(g); g[kid]={"class_type":"KSamplerSelect","inputs":{"sampler_name":p["sampler"]}}
    for _,n in _nodes(g,"SamplerCustomAdvanced"): n.setdefault("inputs",{})["sampler"]=[kid,0]
    for nid,_ in list(_nodes(g,"MiniMaxH3TurboSampler")): g.pop(nid,None)
    if task.get("seed") is not None:
        for _,n in _nodes(g,"RandomNoise"): n.setdefault("inputs",{})["noise_seed"]=int(task["seed"])
    if task.get("mp") is not None:
        for _,n in _nodes(g,"ResolutionSelector"): n.setdefault("inputs",{})["megapixels"]=float(task["mp"])
    if task.get("prefix"):
        for _,n in _nodes(g,"SaveVideo"): n.setdefault("inputs",{})["filename_prefix"]=task["prefix"]

def _image(g, filename):
    filename=str(filename or "").strip()
    for nid,n in _nodes(g,"LoadImage"):
        if str((n.get("inputs") or {}).get("image") or "").strip()==filename: return nid
    nid=_new_id(g); g[nid]={"class_type":"LoadImage","inputs":{"image":filename}}
    return nid

def _length(seconds):
    try: n=max(5, round(float(seconds or 5)*24))
    except (TypeError,ValueError): n=124
    while n % 17 != 5: n += 1
    return n

def _conditioning(g, task, mode):
    prompt=str(task.get("prompt") or ""); length=_length(task.get("duration"))
    if mode in ("t2va","i2va","fl2va"):
        rows=_nodes(g,"MiniMaxH3ImageToVideo")
        if not rows: raise ValueError(mode+" workflow 缺少 MiniMaxH3ImageToVideo")
        inp=rows[0][1].setdefault("inputs",{}); inp["prompt"]=prompt; inp["length"]=length
        if mode=="t2va":
            inp.pop("first_frame",None); inp.pop("last_frame",None); return
        first=str(task.get("image") or "").strip()
        if not first: raise ValueError(mode+" 需要首帧")
        inp["first_frame"]=[_image(g,first),0]
        if mode=="fl2va":
            last=str(task.get("last_frame") or "").strip()
            if not last: raise ValueError("FL2VA 需要真实尾帧；没有尾帧时使用 I2VA")
            inp["last_frame"]=[_image(g,last),0]
        else:
            inp.pop("last_frame",None)
        return
    rows=_nodes(g,"MiniMaxH3ReferenceToVideo")
    if not rows: raise ValueError("Ref2VA workflow 缺少 MiniMaxH3ReferenceToVideo")
    inp=rows[0][1].setdefault("inputs",{}); inp["prompt"]=prompt; inp["length"]=length
    for k in list(inp):
        if str(k).startswith("ref_images.ref_image_"): inp.pop(k,None)
    images=[str(x).strip() for x in (task.get("images") or []) if str(x).strip()]
    if not images: raise ValueError("Ref2VA 需要参考图")
    for i,name in enumerate(images[:9]): inp["ref_images.ref_image_%d"%i]=[_image(g,name),0]

def adapt_graph(graph, task, mode=None):
    mode=ALIASES.get(str(mode or resolve_mode(task)).lower(), str(mode or resolve_mode(task)).lower())
    if mode not in PROFILES: raise ValueError("不支持的 H3 模式: "+mode)
    out=copy.deepcopy(graph); p=_profile(mode,task)
    _strip_accel(out); _model_chain(out,p); _common(out,task,p); _conditioning(out,task,mode)
    left=sorted({n.get("class_type") for n in out.values()
                 if isinstance(n,dict) and n.get("class_type") in FORBIDDEN})
    if left: raise ValueError("稳定 H3 profile 仍残留禁止节点: "+", ".join(left))
    return out

def describe_task(task):
    mode=resolve_mode(task); p=_profile(mode,task)
    return {"mode":mode,"profile":p,"first_frame":task.get("image") or "",
            "last_frame":task.get("last_frame") or "",
            "reference_images":list(task.get("images") or [])}
