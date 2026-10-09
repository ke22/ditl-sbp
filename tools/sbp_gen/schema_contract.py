"""Render a generation stage's mechanical output contract from its schema.

The prompts under `prompts/` used to carry a hand-written copy of each
schema's field list.  That copy was only ever extended when a real run got a
field wrong, so it described the failures we had already seen rather than the
contract itself, and nothing forced it to change when a schema did.

This module removes the second copy: the field mechanics are read from the
same schema file that validates the stage's output, so a schema edit reaches
the model with no prose edit.  What stays hand-written in the `.md` is what a
JSON Schema cannot carry — when to use a field, and why.
"""
from __future__ import annotations

import json
from pathlib import Path

import validate_sbp as V


class SchemaContractError(RuntimeError):
    """A contract could not be rendered in full.

    Raised rather than returning partial prose: a silently truncated contract
    is the exact failure this module exists to prevent.
    """


# 條件式關鍵字刻意不渲染。service-action-graph 用 if/then/not 表達的是語意規則
# （action node 才需要 actor_boundary、planned 動作要填 committed_actor 不填
# actor），機械展開會比 structure.md 現有的對應表更難讀。產生器負責「有哪些
# 欄位、能填什麼值」，`.md` 負責「什麼時候用、為什麼」。
CONDITIONAL_KEYWORDS = ("if", "then", "else", "allOf", "anyOf", "oneOf", "not")

_ROOT_SECTION = "頂層物件"

_TYPE_WORDS = {
    "string": "字串",
    "integer": "整數",
    "number": "數字",
    "boolean": "布林值",
    "array": "陣列",
    "object": "物件",
    "null": "空值",
}


# Schema extension keywords (ignored by JSON Schema validation): a property
# kept only for reading older artifacts, and enum values kept for the same
# reason. Neither appears in the contract the model is shown.
COMPATIBILITY_ONLY_KEYWORD = "x-compatibility-only"
COMPATIBILITY_ONLY_VALUES_KEYWORD = "x-compatibility-only-values"
# cjm-experience-and-moment-solutions (2026-10-01): a property the system or a
# later step writes (a researcher choice, a later generation's output). It is
# stored and validated like any field, but this generation never writes it.
NOT_GENERATED_KEYWORD = "x-not-generated"


def _hidden_from_model(prop: object) -> bool:
    return isinstance(prop, dict) and bool(prop.get(COMPATIBILITY_ONLY_KEYWORD) or prop.get(NOT_GENERATED_KEYWORD))


def render(schema_kind: str, definition: str | None = None) -> str:
    """Render the contract for a `validate_sbp.SCHEMA_FILES` key.

    `definition` roots the contract at one of the schema's definitions, for a
    schema whose root is a `oneOf` of versions: the renderer does not expand
    `oneOf`, so rendering such a root would list no fields at all while still
    telling the model to output nothing that is not listed.
    """
    filename = V.SCHEMA_FILES.get(schema_kind)
    if filename is None:
        raise SchemaContractError(
            f"未註冊的 schema 名稱：{schema_kind!r}（可用的是 {sorted(V.SCHEMA_FILES)}）"
        )
    path = V.SCHEMA_DIR / filename
    if not path.exists():
        raise SchemaContractError(f"schema 檔案不存在：{path}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    if definition is not None:
        definitions = schema.get("definitions") or {}
        if definition not in definitions:
            raise SchemaContractError(f"{filename} 沒有 definition {definition!r}")
        schema = {**definitions[definition], "definitions": definitions}
    return render_loaded(schema, filename)


def render_loaded(schema: dict, stem: str) -> str:
    """Render the contract for an already-loaded schema.

    `stem` is the schema's filename, used for the header and to resolve
    same-file references.  Taking a loaded mapping is what lets a test render
    a modified copy of a schema without writing to `schema/`.
    """
    return _Renderer(schema, stem).run()


class _Renderer:
    """Walks properties / items / $ref edges once and emits one section per object.

    Objects are keyed by (filename, JSON pointer) so an object reached twice —
    including through a cycle — refers back to the section already emitted
    instead of recursing.
    """

    def __init__(self, schema: dict, stem: str):
        self.stem = stem
        # 根 schema 以傳進來的內容註冊，不是磁碟上的版本：測試才能渲染改過的
        # 副本，而 `#/definitions/...` 仍然解析到同一份。
        self.documents: dict[str, dict] = {stem: schema}
        self.names: dict[tuple[str, str], str] = {}
        self.used_names: set[str] = set()
        self.queue: list[tuple[dict, str, str]] = []
        self.sections: list[str] = []

        self.names[(stem, "#")] = _ROOT_SECTION
        self.used_names.add(_ROOT_SECTION)
        self.queue.append((schema, _ROOT_SECTION, stem))

    def run(self) -> str:
        lines = [
            f"## 輸出欄位契約（由 {self.stem} 自動產生，不要手動修改）",
            "",
            "以下欄位規則直接來自驗證這份輸出的 schema。沒有列在這裡的欄位一律不要輸出。",
            "",
        ]
        while self.queue:
            node, name, stem = self.queue.pop(0)
            lines.extend(self._render_object(node, name, stem))
        return "\n".join(lines).rstrip() + "\n"

    # -- object rendering -------------------------------------------------

    def _render_object(self, node: dict, name: str, stem: str) -> list[str]:
        # model-facing-current-fields-only (2026-10-01): a field kept only so
        # older artifacts stay readable is not part of what the model may
        # write. Storage validation still reads the full schema.
        properties = {key: prop for key, prop in (node.get("properties") or {}).items()
                      if not _hidden_from_model(prop)}
        # 這份契約只給生成階段看，所以 `x-generation-required` 在這裡就是必填——
        # 跟 validate_sbp.generation_schema() 折疊的是同一組欄位，同一個來源。
        declared = list(node.get("required") or [])
        declared += [key for key in (node.get(V.GENERATION_REQUIRED_KEYWORD) or []) if key not in declared]
        required = [key for key in declared if key in properties]
        optional = [key for key in properties if key not in required]

        lines = [f"### {name}", ""]
        lines.append(_field_list_line("必填欄位", required))
        lines.append(_field_list_line("選填欄位", optional))
        if node.get("additionalProperties") is False:
            lines.append("- 不接受上面沒列到的任何欄位。")
        lines.append("")
        for key, prop in properties.items():
            lines.append(f"- `{key}`：{self._describe(prop, name, key, stem)}")
        lines.append("")
        return lines

    # -- property description ---------------------------------------------

    def _describe(self, prop: dict, parent: str, key: str, stem: str) -> str:
        if "oneOf" in prop:
            # A oneOf kept for reading older shapes: once its compatibility-only
            # variants are hidden, a single current variant is the contract.
            current = [variant for variant in prop["oneOf"] if not _hidden_from_model(variant)]
            if len(current) == 1:
                return self._describe(current[0], parent, key, stem)
        if "$ref" in prop:
            target, target_stem, pointer = self._resolve(prop["$ref"], stem)
            if _is_object(target):
                section = self._register(target, target_stem, pointer, parent, key)
                return f"物件，見「{section}」"
            # 純量的 $ref（例如 evidence_id）直接把限制攤開，不另開一節。
            return self._describe(target, parent, key, target_stem)

        if "const" in prop:
            return f"固定填 {_literal(prop['const'])}"
        if "enum" in prop:
            hidden = set(prop.get(COMPATIBILITY_ONLY_VALUES_KEYWORD) or ())
            values = "、".join(_literal(value) for value in prop["enum"] if value not in hidden)
            return f"只能填 {values} 其中一個"

        parts: list[str] = []
        declared = prop.get("type")
        if isinstance(declared, list):
            parts.append("或".join(_TYPE_WORDS.get(item, item) for item in declared))
        elif declared:
            parts.append(_TYPE_WORDS.get(declared, declared))
        elif _is_object(prop):
            parts.append(_TYPE_WORDS["object"])

        if prop.get("minLength"):
            parts.append(f"至少 {prop['minLength']} 個字")
        if prop.get("maxLength"):
            parts.append(f"最多 {prop['maxLength']} 個字")
        if "minimum" in prop and "maximum" in prop:
            parts.append(f"{prop['minimum']} 到 {prop['maximum']}")
        if prop.get("minItems"):
            parts.append(f"至少 {prop['minItems']} 項")
        if prop.get("maxItems"):
            parts.append(f"最多 {prop['maxItems']} 項")
        if prop.get("uniqueItems"):
            parts.append("各項不重複")
        if prop.get("pattern"):
            parts.append(f"格式要符合 {prop['pattern']}")

        described = "、".join(parts) if parts else "沒有額外限制"

        items = prop.get("items")
        if isinstance(items, dict):
            described += "；每一項" + self._describe_item(items, parent, key, stem)
        elif _is_object(prop):
            section = self._register(
                prop, stem, None, parent, key, fallback=_child_name(parent, key)
            )
            described += f"，見「{section}」"

        return described

    def _describe_item(self, items: dict, parent: str, key: str, stem: str) -> str:
        if "$ref" in items:
            target, target_stem, pointer = self._resolve(items["$ref"], stem)
            if _is_object(target):
                section = self._register(target, target_stem, pointer, parent, key)
                return f"見「{section}」"
            return "是" + self._describe(target, parent, key, target_stem)
        if _is_object(items):
            section = self._register(
                items, stem, None, parent, key, fallback=_child_name(parent, key)
            )
            return f"見「{section}」"
        return "是" + self._describe(items, parent, key, stem)

    # -- naming and registration -------------------------------------------

    def _register(
        self,
        node: dict,
        stem: str,
        pointer: str | None,
        parent: str,
        key: str,
        fallback: str | None = None,
    ) -> str:
        """Name an object section, queueing it the first time it is seen."""
        identity = (stem, pointer if pointer is not None else f"inline:{parent}/{key}")
        existing = self.names.get(identity)
        if existing is not None:
            return existing

        candidate = fallback or (pointer or "").rsplit("/", 1)[-1] or _child_name(parent, key)
        name = candidate
        if name in self.used_names:
            # 不同檔案撞名時掛上檔名前綴，讓兩節都指得回自己的來源。
            name = f"{Path(stem).name.split('.')[0]}.{candidate}"
        suffix = 2
        while name in self.used_names:
            name = f"{candidate}-{suffix}"
            suffix += 1

        self.names[identity] = name
        self.used_names.add(name)
        self.queue.append((node, name, stem))
        return name

    # -- reference resolution ----------------------------------------------

    def _resolve(self, ref: str, stem: str) -> tuple[dict, str, str]:
        """Resolve a local or cross-file `$ref` to (node, filename, pointer).

        Mirrors what `validate_sbp.structural_errors()` does: sibling schema
        files are read from `SCHEMA_DIR`, and only filenames registered in
        `SCHEMA_FILES` are accepted, so a stray reference fails loudly instead
        of silently dropping fields from the contract.
        """
        filename, _, pointer = ref.partition("#")
        target_stem = filename or stem
        document = self._document(target_stem, ref)

        node: object = document
        for raw in [part for part in pointer.split("/") if part]:
            part = raw.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise SchemaContractError(
                    f"schema 參照無法解析：{ref!r}（在 {target_stem} 裡找不到 {part!r}）"
                )
            node = node[part]
        if not isinstance(node, dict):
            raise SchemaContractError(f"schema 參照沒有指向物件：{ref!r}")
        return node, target_stem, f"#{pointer}"

    def _document(self, filename: str, ref: str) -> dict:
        cached = self.documents.get(filename)
        if cached is not None:
            return cached
        if filename not in set(V.SCHEMA_FILES.values()):
            raise SchemaContractError(
                f"schema 參照指向未註冊的檔案：{ref!r}（{filename} 不在 SCHEMA_FILES 裡）"
            )
        path = V.SCHEMA_DIR / filename
        if not path.exists():
            raise SchemaContractError(f"schema 參照指向不存在的檔案：{path}")
        document = json.loads(path.read_text(encoding="utf-8"))
        self.documents[filename] = document
        return document


def _is_object(node: dict) -> bool:
    return node.get("type") == "object" or "properties" in node


def _child_name(parent: str, key: str) -> str:
    if parent == _ROOT_SECTION:
        return key
    return f"{parent}.{key}"


def _field_list_line(label: str, keys: list[str]) -> str:
    if not keys:
        return f"- {label}：無"
    names = "、".join(f"`{key}`" for key in keys)
    return f"- {label}（{len(keys)} 個）：{names}"


def _literal(value) -> str:
    if isinstance(value, bool):
        return f"`{'true' if value else 'false'}`"
    return f"`{value}`"
