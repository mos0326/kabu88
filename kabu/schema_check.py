"""最小限のJSONスキーマ検証（Claude Code が書いた監査JSONのチェック用。追加の依存なし）。"""

from __future__ import annotations


def validate(obj, schema: dict, path: str = "$") -> list[str]:
    """エラーメッセージのリストを返す（空なら合格）。"""
    errs: list[str] = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(obj, dict):
            return [f"{path}: object が必要です"]
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in obj:
                errs.append(f"{path}.{k}: 必須項目がありません")
        if schema.get("additionalProperties") is False:
            errs += [f"{path}.{k}: スキーマにない項目です" for k in obj if k not in props]
        for k, v in obj.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
    elif t == "array":
        if not isinstance(obj, list):
            return [f"{path}: array が必要です"]
        for i, v in enumerate(obj):
            errs += validate(v, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string" and not isinstance(obj, str):
        errs.append(f"{path}: string が必要です")
    elif t == "integer" and not (isinstance(obj, int) and not isinstance(obj, bool)):
        errs.append(f"{path}: integer が必要です")
    elif t == "number" and not (isinstance(obj, (int, float)) and not isinstance(obj, bool)):
        errs.append(f"{path}: number が必要です")
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} は {schema['enum']} のいずれかにしてください")
    return errs
