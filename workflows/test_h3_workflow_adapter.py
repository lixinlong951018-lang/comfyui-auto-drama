#!/usr/bin/env python3
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h3_workflow_adapter as h3a


def sample_graph():
    return {
        "1": {"class_type":"UNETLoader","inputs":{"unet_name":"old.safetensors","weight_dtype":"default"}},
        "2": {"class_type":"MiniMaxH3TurboLoRA","inputs":{"model":["1",0],"lora_name":"turbo.safetensors","strength":1.0}},
        "3": {"class_type":"MiniMaxH3MemoryEfficientSageAttentionPatch","inputs":{"model":["2",0]}},
        "4": {"class_type":"EasyCache","inputs":{"model":["3",0],"reuse_threshold":0.2}},
        "5": {"class_type":"BasicScheduler","inputs":{"model":["4",0],"scheduler":"simple","steps":4,"denoise":1}},
        "6": {"class_type":"BasicGuider","inputs":{"model":["4",0],"conditioning":["12",0]}},
        "7": {"class_type":"KSamplerSelect","inputs":{"sampler_name":"res_multistep"}},
        "8": {"class_type":"SamplerCustomAdvanced","inputs":{"guider":["6",0],"sampler":["7",0],"sigmas":["5",0],"latent_image":["12",1]}},
        "9": {"class_type":"VAELoader","inputs":{"vae_name":"minimax_h3_video_vae_fp16.safetensors"}},
        "10":{"class_type":"VAELoader","inputs":{"vae_name":"minimax_h3_audio_vae_fp32.safetensors"}},
        "11":{"class_type":"CLIPLoader","inputs":{"clip_name":"old_clip.safetensors","type":"minimax","device":"default"}},
        "12":{"class_type":"MiniMaxH3ImageToVideo","inputs":{"prompt":"old","clip":["11",0],"vae":["9",0],"first_frame":["13",0],"width":864,"height":480,"length":124}},
        "13":{"class_type":"LoadImage","inputs":{"image":"first.png"}},
        "14":{"class_type":"RandomNoise","inputs":{"noise_seed":1}},
        "15":{"class_type":"SaveVideo","inputs":{"filename_prefix":"old","video":["16",0]}},
    }


class AdapterTests(unittest.TestCase):
    def test_i2va_stable_profile(self):
        graph=h3a.adapt_graph(sample_graph(),{
            "mode":"i2v","image":"first.png","prompt":"hello",
            "duration":5,"seed":666,"prefix":"video/test",
        })
        types=[n["class_type"] for n in graph.values()]
        self.assertNotIn("MiniMaxH3TurboLoRA",types)
        self.assertNotIn("MiniMaxH3MemoryEfficientSageAttentionPatch",types)
        self.assertNotIn("EasyCache",types)
        self.assertIn("PathchSageAttentionKJ",types)
        self.assertIn("MiniMaxH3SigmaShift",types)
        cond=next(n for n in graph.values() if n["class_type"]=="MiniMaxH3ImageToVideo")
        self.assertIn("first_frame",cond["inputs"])
        self.assertNotIn("last_frame",cond["inputs"])
        sched=next(n for n in graph.values() if n["class_type"]=="BasicScheduler")
        self.assertEqual(sched["inputs"]["steps"],8)
        vae=next(n for n in graph.values()
                 if n["class_type"]=="VAELoader" and "audio" not in n["inputs"]["vae_name"])
        self.assertEqual(vae["inputs"]["vae_name"],"minimax_h3_video_vae_int8_convrot.safetensors")

    def test_fl2va_requires_real_last_frame(self):
        graph=h3a.adapt_graph(sample_graph(),{
            "mode":"fl2va","image":"first.png","last_frame":"last.png",
            "prompt":"hello","duration":5,"seed":666,
        })
        cond=next(n for n in graph.values() if n["class_type"]=="MiniMaxH3ImageToVideo")
        self.assertIn("last_frame",cond["inputs"])
        image_id=str(cond["inputs"]["last_frame"][0])
        self.assertEqual(graph[image_id]["inputs"]["image"],"last.png")

    def test_legacy_i2v_plus_tail_resolves_fl2va(self):
        self.assertEqual(
            h3a.resolve_mode({"mode":"i2v","image":"a.png","last_frame":"b.png"}),
            "fl2va",
        )

    def test_i2va_has_no_placeholder_tail(self):
        graph=h3a.adapt_graph(sample_graph(),{
            "mode":"i2v","image":"first.png","last_frame":"",
            "prompt":"hello","duration":5,
        })
        cond=next(n for n in graph.values() if n["class_type"]=="MiniMaxH3ImageToVideo")
        self.assertNotIn("last_frame",cond["inputs"])


if __name__=="__main__":
    unittest.main()
