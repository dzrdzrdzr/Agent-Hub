#!/usr/bin/env python3
import torch
import os
import json

output_path = "/data6/hanzaidao/2026-7/Codex_Cline/_ckpt_inspect_result.json"

paths = [
    "/data6/hanzaidao/GAUSS_26_4/GAUSS-test/GAUSS/outputs/shapebank_accepted_v4/shape_bank_accepted.pt",
    "/data6/hanzaidao/GAUSS_26_4/GAUSS-test/GAUSS/outputs/shapebank_router_recovery/A4_router_balance05/router_best.pt",
]

results = {}

for p in paths:
    entry = {"file": p, "exists": os.path.exists(p)}
    if not entry["exists"]:
        results[p] = entry
        continue
    try:
        ckpt = torch.load(p, map_location="cpu", weights_only=False)
        entry["top_keys"] = list(ckpt.keys())
        
        if "attention_model_state" in ckpt:
            sd = ckpt["attention_model_state"]
            entry["state_keys"] = {k: {"shape": list(v.shape), "dtype": str(v.dtype)} 
                                   for k, v in sd.items()}
        
        if "bank_raw" in ckpt:
            br = ckpt["bank_raw"]
            entry["bank_raw_shape"] = list(br.shape)
            entry["bank_raw_dtype"] = str(br.dtype)
        
        if "active_prototype_mask" in ckpt:
            am = ckpt["active_prototype_mask"]
            entry["active_mask_shape"] = list(am.shape)
            active = am.sum(dim=1)
            entry["active_per_class"] = {str(c): int(active[c]) for c in range(am.shape[0]) if active[c] > 0}
        
        if "config" in ckpt:
            cfg = ckpt.get("config", {})
            if isinstance(cfg, dict):
                entry["config_keys"] = {k: str(cfg[k]) for k in cfg if k in [
                    "num_gaussians", "num_classes", "max_prototypes", "hidden_dim",
                    "attention_heads", "decoder_layers", "gaussians", "prototypes",
                    "classes", "stage", "run_name", "max_prototypes"
                ]}
        
        if "schema_version" in ckpt:
            entry["schema_version"] = ckpt["schema_version"]
        if "model_type" in ckpt:
            entry["model_type"] = ckpt["model_type"]
        
        results[p] = entry
    except Exception as e:
        entry["error"] = str(e)
        results[p] = entry

with open(output_path, "w") as f:
    json.dump(results, f, indent=2, default=str)

print("DONE - wrote", output_path)

