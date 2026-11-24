"""规范化生成规格与输入指纹（工作台 Stage A2，design D4）。

每个可生成资产在派发/生成时计算：
- generation_spec：完整、规范化的生成规格（直接内容、模型稳定标识、模板版本、
  有效参数、生成器版本、上游依赖指纹）；
- input_fingerprint：规格规范化 JSON 的 SHA-256（64 字符，索引）。

指纹只随语义输入变化（spec: incremental-generation「相同输入的指纹稳定」）：
- JSON 键顺序 / 空白格式变化不影响指纹（canonical dumps：sort_keys + separators）；
- 模型、模板、有效参数、上游依赖 UUID/指纹、生成器版本任一变化 → 指纹变化；
- 随机种子只在显式指定或用户强制重做时进入指纹（未指定时不出现在规格里，
  不会让同输入两次生成得到不同指纹）；
- 响应中的用量、成本、存储地址不进规格（产出侧信息不参与输入指纹）。
"""
import hashlib
import json

# 生成器版本：生成逻辑（构图/适配器参数组装）变化时递增，使旧资产指纹失效
# （不兼容的生成产出不被当成相同输入复用）
GENERATOR_VERSION = 1

# 指纹算法的规格 schema 版本（规格结构本身变化时递增，防止跨版本指纹碰撞）
SPEC_SCHEMA_VERSION = 1


def _canonical(obj) -> str:
    """规范化 JSON 文本：排序键 + 紧凑分隔符——键顺序/空白不影响结果。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_spec(*, target_type, direct_content=None, model=None, template=None,
                   effective_params=None, upstream=None, seed=None) -> dict:
    """构建规范化生成规格（键序无关，值语义化）。

    Args:
        target_type: 资产类型（AssetType 值，如 "IMAGE"）
        direct_content: 直接内容（prompt 文本、对白文本等；dict/list 也接受）
        model: 模型稳定标识（AIModel.pk + model_name，或 provider/model 字符串）
        template: Prompt 模板版本（dict：模板名→版本/内容摘要）
        effective_params: 有效生成参数（resolve_gen_params 的 effective 等）
        upstream: 上游依赖列表 [{uuid, fingerprint, content_sha256}, ...]
        seed: 显式随机种子（None 不进规格——未指定种子的两次生成输入相同）

    Returns:
        规格字典（可直接存 Asset.generation_spec；含 schema_version/generator_version）
    """
    spec = {
        "schema_version": SPEC_SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "target_type": str(target_type),
        "direct_content": direct_content if direct_content is not None else "",
        "model": _normalize_model(model),
        "template": template or {},
        "effective_params": dict(effective_params or {}),
        "upstream": [
            {
                "uuid": str(d.get("uuid", "")),
                "fingerprint": str(d.get("fingerprint", "")),
                "content_sha256": str(d.get("content_sha256", "")),
            }
            for d in (upstream or [])
        ],
    }
    if seed is not None:
        spec["seed"] = seed
    return spec


def _normalize_model(model) -> dict:
    """模型稳定标识：{id, name}；None/缺名 → 空串（规格仍可比较）。"""
    if model is None:
        return {"id": "", "name": ""}
    pk = getattr(model, "pk", None)
    name = getattr(model, "model_name", "") or str(model)
    return {"id": str(pk if pk is not None else ""), "name": str(name)}


def fingerprint_of(spec) -> str:
    """规格 → SHA-256 指纹（64 字符）。spec 为 normalize_spec 产物或等价 dict。"""
    return _sha256_hex(_canonical(spec))


def upstream_refs(assets) -> list:
    """上游 Asset 列表 → 规格引用列表（uuid/指纹/内容摘要）。

    历史 Asset 无指纹时 fingerprint 为空串——下游规格仍可构建，但复用判定
    （generation.py，任务 2.5）会把空指纹视为"不可证明相同输入"而保守重建。
    """
    refs = []
    for a in assets or []:
        refs.append({
            "uuid": str(a.uuid) if getattr(a, "uuid", None) else "",
            "fingerprint": a.input_fingerprint or "",
            "content_sha256": a.content_sha256 or "",
        })
    return refs
