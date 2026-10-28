"""The ComfyUI workflows the worker runs, built as API graphs in code.

Each workflow names the model files it needs (matched against what ComfyUI
has, so a renamed download still counts), whether it takes reference
pictures, and how big it makes each picture type. Sizes are the models' own:
FLUX.2 makes about two megapixels in any shape; SDXL is best near one
megapixel, so its pictures are smaller.
"""

import re
from typing import ClassVar

KINDS = ["Primary", "Thumb", "Backdrop", "Logo", "Banner"]


class Workflow:
    key = ""
    label = ""
    references = False
    # {loader: (input, [patterns])}: the first file matching a pattern is used.
    needs: ClassVar[dict] = {}
    sizes: ClassVar[dict] = {
        "Primary": (1024, 1536),
        "Thumb": (1920, 1088),
        "Backdrop": (1920, 1088),
        "Banner": (1728, 320),
        "Logo": (1536, 768),
    }
    seconds = 30  # about how long a picture takes, for progress
    one_person = False  # it can only keep one person's likeness

    def resolve(self, available: dict[str, list[str]]) -> tuple[dict | None, str]:
        """{loader: file} for this workflow from ComfyUI's lists, or None
        and what's missing."""
        chosen = {}
        for loader, (_input, patterns) in self.needs.items():
            files = available.get(loader) or []
            match = next(
                (f for p in patterns for f in files if re.search(p, f, re.IGNORECASE)),
                None,
            )
            if match is None:
                return None, f"needs a {loader} model like {patterns[0].strip('^$')}"
            chosen[loader] = match
        return chosen, ""

    def size(self, kind: str) -> tuple[int, int]:
        return self.sizes[kind]

    def graph(self, prompt, size, seed, refs, models) -> dict:
        raise NotImplementedError


def _save(graph: dict, image) -> dict:
    graph["save"] = {
        "class_type": "SaveImage",
        "inputs": {"images": image, "filename_prefix": "curatarr"},
    }
    return graph


class Flux2Klein(Workflow):
    """FLUX.2 [klein] 4B, distilled: four steps, and reference images as
    extra latents (FLUX.2's own way of keeping a person or a style)."""

    key = "comfyui/flux2-klein-4b"
    label = "FLUX.2 Klein 4B (local)"
    references = True
    needs: ClassVar[dict] = {
        "UNETLoader": ("unet_name", [r"flux-?2-klein-4b(?!.*base)"]),
        "CLIPLoader": ("clip_name", [r"^qwen_?3_?4b\.", r"qwen.?3.?4b(?!.*vl)"]),
        "VAELoader": ("vae_name", [r"flux2.*vae", r"flux-2.*vae"]),
    }
    steps = 4
    cfg = 1.0
    seconds = 15

    def graph(self, prompt, size, seed, refs, models):
        width, height = size
        g = {
            "unet": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": models["UNETLoader"],
                    "weight_dtype": "default",
                },
            },
            "clip": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": models["CLIPLoader"],
                    "type": "flux2",
                    "device": "default",
                },
            },
            "vae": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": models["VAELoader"]},
            },
            "pos": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["clip", 0]},
            },
            "latent": {
                "class_type": "EmptyFlux2LatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
            "sched": {
                "class_type": "Flux2Scheduler",
                "inputs": {"steps": self.steps, "width": width, "height": height},
            },
            "sampler": {
                "class_type": "KSamplerSelect",
                "inputs": {"sampler_name": "euler"},
            },
            "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        }
        g.update(self.negative())
        positive, negative = ["pos", 0], ["neg", 0]
        for i, name in enumerate(refs):
            g[f"ref{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            g[f"refscale{i}"] = {
                "class_type": "ImageScaleToTotalPixels",
                "inputs": {
                    "image": [f"ref{i}", 0],
                    "upscale_method": "lanczos",
                    "megapixels": 1.0,
                    "resolution_steps": 16,
                },
            }
            g[f"reflatent{i}"] = {
                "class_type": "VAEEncode",
                "inputs": {"pixels": [f"refscale{i}", 0], "vae": ["vae", 0]},
            }
            g[f"pos{i}"] = {
                "class_type": "ReferenceLatent",
                "inputs": {"conditioning": positive, "latent": [f"reflatent{i}", 0]},
            }
            g[f"neg{i}"] = {
                "class_type": "ReferenceLatent",
                "inputs": {"conditioning": negative, "latent": [f"reflatent{i}", 0]},
            }
            positive, negative = [f"pos{i}", 0], [f"neg{i}", 0]
        g["guider"] = {
            "class_type": "CFGGuider",
            "inputs": {
                "model": ["unet", 0],
                "positive": positive,
                "negative": negative,
                "cfg": self.cfg,
            },
        }
        g["sample"] = {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["noise", 0],
                "guider": ["guider", 0],
                "sampler": ["sampler", 0],
                "sigmas": ["sched", 0],
                "latent_image": ["latent", 0],
            },
        }
        g["decode"] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]},
        }
        return _save(g, ["decode", 0])

    def negative(self) -> dict:
        return {
            "neg": {
                "class_type": "ConditioningZeroOut",
                "inputs": {"conditioning": ["pos", 0]},
            }
        }


class Flux2KleinBase(Flux2Klein):
    """FLUX.2 [klein] 4B base (not distilled): many more steps with real
    guidance; slower, sometimes more faithful to the prompt."""

    key = "comfyui/flux2-klein-4b-base"
    label = "FLUX.2 Klein 4B base (local, slower)"
    needs: ClassVar[dict] = Flux2Klein.needs | {
        "UNETLoader": (
            "unet_name",
            [r"flux-?2-klein-base-4b", r"flux-?2-klein-4b.*base"],
        ),
    }
    steps = 28
    cfg = 4.0
    seconds = 90

    def negative(self):
        return {
            "neg": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": "blurry, low quality, distorted faces, extra fingers, "
                    "misspelled text, watermark, credits block",
                    "clip": ["clip", 0],
                },
            }
        }


class Flux2Klein9B(Flux2Klein):
    """FLUX.2 [klein] 9B, distilled: the larger Klein, when it's installed."""

    key = "comfyui/flux2-klein-9b"
    label = "FLUX.2 Klein 9B (local)"
    needs: ClassVar[dict] = {
        "UNETLoader": ("unet_name", [r"flux-?2-klein-9b(?!.*base)"]),
        "CLIPLoader": ("clip_name", [r"qwen.?3.?8b"]),
        "VAELoader": ("vae_name", [r"flux2.*vae", r"flux-2.*vae"]),
    }
    seconds = 30


class Krea2Turbo(Workflow):
    """Krea 2 turbo: text only (no reference pictures), eight steps."""

    key = "comfyui/krea2-turbo"
    label = "Krea 2 Turbo (local, no reference photos)"
    references = False
    needs: ClassVar[dict] = {
        "UNETLoader": ("unet_name", [r"krea2.*turbo"]),
        "CLIPLoader": ("clip_name", [r"qwen3vl"]),
        "VAELoader": ("vae_name", [r"qwen_image_vae", r"qwen.*vae"]),
    }
    sizes: ClassVar[dict] = {
        "Primary": (1024, 1536),
        "Thumb": (1792, 1008),
        "Backdrop": (1792, 1008),
        "Banner": (1728, 320),
        "Logo": (1536, 768),
    }
    seconds = 40

    def graph(self, prompt, size, seed, refs, models):
        width, height = size
        g = {
            "unet": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": models["UNETLoader"],
                    "weight_dtype": "default",
                },
            },
            "clip": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": models["CLIPLoader"],
                    "type": "krea2",
                    "device": "default",
                },
            },
            "vae": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": models["VAELoader"]},
            },
            "pos": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["clip", 0]},
            },
            "neg": {
                "class_type": "ConditioningZeroOut",
                "inputs": {"conditioning": ["pos", 0]},
            },
            "latent": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
            "sample": {
                "class_type": "KSampler",
                "inputs": {
                    "model": ["unet", 0],
                    "seed": seed,
                    "steps": 8,
                    "cfg": 1.0,
                    "sampler_name": "euler",
                    "scheduler": "simple",
                    "positive": ["pos", 0],
                    "negative": ["neg", 0],
                    "latent_image": ["latent", 0],
                    "denoise": 1.0,
                },
            },
            "decode": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]},
            },
        }
        return _save(g, ["decode", 0])


class SdxlFaceId(Workflow):
    """SDXL (a photographic checkpoint) with IP-Adapter FaceID Plus v2: a
    face embedding from the reference photos steers every picture. It keeps
    one person's likeness (two people blend into one), and SDXL can't spell
    titles reliably."""

    key = "comfyui/sdxl-faceid"
    one_person = True
    label = "SDXL + IP-Adapter FaceID (local, one person)"
    references = True
    needs: ClassVar[dict] = {
        "CheckpointLoaderSimple": ("ckpt_name", [r"juggernaut.*xl", r"xl"]),
        "IPAdapterFaceID": ("_", [r"."]),  # the custom nodes are installed
    }
    sizes: ClassVar[dict] = {
        "Primary": (832, 1216),
        "Thumb": (1344, 768),
        "Backdrop": (1344, 768),
        "Banner": (1536, 320),
        "Logo": (1344, 704),
    }
    seconds = 35

    def graph(self, prompt, size, seed, refs, models):
        width, height = size
        g = {
            "ckpt": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": models["CheckpointLoaderSimple"]},
            },
            "pos": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["ckpt", 1]},
            },
            "neg": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": "blurry, lowres, deformed, bad anatomy, extra fingers, "
                    "watermark, signature, jpeg artifacts",
                    "clip": ["ckpt", 1],
                },
            },
            "latent": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
        }
        model = ["ckpt", 0]
        if refs:
            g["faceid"] = {
                "class_type": "IPAdapterUnifiedLoaderFaceID",
                "inputs": {
                    "model": model,
                    "preset": "FACEID PLUS V2",
                    "lora_strength": 0.6,
                    "provider": "CPU",
                },
            }
            image = None
            for i, name in enumerate(refs):
                g[f"ref{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
                if image is None:
                    image = [f"ref{i}", 0]
                else:
                    g[f"batch{i}"] = {
                        "class_type": "ImageBatch",
                        "inputs": {"image1": image, "image2": [f"ref{i}", 0]},
                    }
                    image = [f"batch{i}", 0]
            g["apply"] = {
                "class_type": "IPAdapterFaceID",
                "inputs": {
                    "model": ["faceid", 0],
                    "ipadapter": ["faceid", 1],
                    "image": image,
                    "weight": 0.85,
                    "weight_faceidv2": 1.0,
                    "weight_type": "linear",
                    "combine_embeds": "average",
                    "start_at": 0.0,
                    "end_at": 1.0,
                    "embeds_scaling": "V only",
                },
            }
            model = ["apply", 0]
        g["sample"] = {
            "class_type": "KSampler",
            "inputs": {
                "model": model,
                "seed": seed,
                "steps": 35,
                "cfg": 5.0,
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "positive": ["pos", 0],
                "negative": ["neg", 0],
                "latent_image": ["latent", 0],
                "denoise": 1.0,
            },
        }
        g["decode"] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["sample", 0], "vae": ["ckpt", 2]},
        }
        return _save(g, ["decode", 0])


WORKFLOWS = [Flux2Klein(), Flux2Klein9B(), Flux2KleinBase(), Krea2Turbo(), SdxlFaceId()]
