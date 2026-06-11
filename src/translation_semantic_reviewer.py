"""Optional AI semantic reviewer for generated translation changes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import jsonschema
import yaml
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionSystemMessageParam, ChatCompletionUserMessageParam

from src.semantic_quality import (
    TranslationChange,
    iter_translation_changes_from_diff,
)
from src.translation_quality_gate import get_staged_diff


SEMANTIC_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file", "key", "severity", "reason"],
                "properties": {
                    "file": {"type": "string"},
                    "key": {"type": "string"},
                    "severity": {"type": "string", "enum": ["error", "warning"]},
                    "reason": {"type": "string"},
                    "suggested_value": {"type": "string"},
                },
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def build_semantic_review_messages(
    target_language: str,
    changes: Sequence[TranslationChange],
    style_rules: Sequence[str],
    brand_glossary: Sequence[str],
) -> List[Dict[str, str]]:
    scoped_changes = [
        {
            "file": change.file,
            "key": change.key,
            "source_value": change.source_value or "",
            "old_target_value": change.old_value or "",
            "new_target_value": change.new_value,
        }
        for change in changes
    ]
    system_prompt = (
        "You are an independent semantic QA reviewer for software localization. "
        "Review only the provided changed keys. Return JSON only. Do not return markdown, "
        "explanations, or corrected translations outside the JSON schema."
    )
    user_payload = {
        "task": "Find semantic translation issues that deterministic checks may miss.",
        "target_language": target_language,
        "style_rules": list(style_rules),
        "brand_glossary": list(brand_glossary),
        "allowed_severities": ["error", "warning"],
        "response_schema": {
            "findings": [
                {
                    "file": "relative/path/to/file.properties",
                    "key": "changed.key.only",
                    "severity": "error|warning",
                    "reason": "Short reviewer rationale.",
                    "suggested_value": "Optional corrected value.",
                }
            ]
        },
        "changes": scoped_changes,
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
    ]


def normalize_review_response(
    response_text: str,
    changes: Sequence[TranslationChange],
) -> List[Dict[str, str]]:
    parsed = json.loads(response_text)
    jsonschema.validate(instance=parsed, schema=SEMANTIC_REVIEW_SCHEMA)
    changes_by_identity = {(change.file, change.key): change for change in changes}
    findings: List[Dict[str, str]] = []

    for raw_finding in parsed.get("findings", []):
        file = raw_finding["file"]
        key = raw_finding["key"]
        change = changes_by_identity.get((file, key))
        if not change:
            continue
        finding = {
            "file": change.file,
            "key": key,
            "severity": raw_finding["severity"],
            "reason": raw_finding["reason"],
            "value": change.new_value,
            "source": "ai-review",
            "rule_id": "ai-review",
        }
        if raw_finding.get("suggested_value"):
            finding["suggested_value"] = raw_finding["suggested_value"]
        findings.append(finding)
    return findings


def append_semantic_review_findings(
    validation_summary_path: str,
    findings: Sequence[Dict[str, str]],
) -> None:
    if os.path.exists(validation_summary_path):
        with open(validation_summary_path, "r", encoding="utf-8") as file:
            summary = json.load(file)
    else:
        summary = {"files": {}, "pipeline_warnings": []}

    summary.setdefault("files", {})
    summary.setdefault("pipeline_warnings", [])
    summary.setdefault("semantic_review_findings", [])
    summary["semantic_review_findings"].extend(findings)

    Path(validation_summary_path).parent.mkdir(parents=True, exist_ok=True)
    with open(validation_summary_path, "w", encoding="utf-8") as file:
        json.dump(summary, file, ensure_ascii=False, indent=2)


async def review_translation_changes(
    client: AsyncOpenAI,
    model: str,
    target_language: str,
    changes: Sequence[TranslationChange],
    style_rules: Sequence[str],
    brand_glossary: Sequence[str],
) -> List[Dict[str, str]]:
    if not changes:
        return []
    messages = build_semantic_review_messages(
        target_language=target_language,
        changes=changes,
        style_rules=style_rules,
        brand_glossary=brand_glossary,
    )
    response = await client.chat.completions.create(
        model=model,
        messages=[
            ChatCompletionSystemMessageParam(role="system", content=messages[0]["content"]),
            ChatCompletionUserMessageParam(role="user", content=messages[1]["content"]),
        ],
        temperature=0,
        response_format={"type": "json_object"},
        max_tokens=4096,
        timeout=120.0,
    )
    response_text = response.choices[0].message.content or ""
    return normalize_review_response(response_text, changes)


def _load_config(config_path: str) -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--input-folder", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--validation-summary", required=True)
    parser.add_argument("--changed-files", nargs="+", required=True)
    return parser.parse_args(argv)


async def _run(argv: Optional[Sequence[str]]) -> int:
    args = _parse_args(argv)
    config = _load_config(args.config)
    semantic_review_config = config.get("semantic_review", {}) or {}
    if not bool(semantic_review_config.get("enabled", False)):
        return 0
    if bool(config.get("dry_run", False)):
        append_semantic_review_findings(args.validation_summary, [])
        return 0

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return 0

    locales = [
        locale["code"]
        for locale in config.get("supported_locales", [])
        if isinstance(locale, dict) and locale.get("code")
    ]
    language_names = {
        locale["code"]: locale.get("name", locale["code"])
        for locale in config.get("supported_locales", [])
        if isinstance(locale, dict) and locale.get("code")
    }
    diff_text = get_staged_diff(args.repo_root, args.changed_files)
    changes = list(
        iter_translation_changes_from_diff(
            diff_text=diff_text,
            repo_root=args.repo_root,
            input_folder=args.input_folder,
            locale_codes=locales,
        )
    )
    if not changes:
        append_semantic_review_findings(args.validation_summary, [])
        return 0

    brand_glossary = [str(term) for term in config.get("brand_technical_glossary", [])]
    style_rules_by_locale = config.get("style_rules", {}) or {}
    model = str(
        semantic_review_config.get(
            "model",
            config.get("review_model_name", config.get("model_name", "gpt-4o")),
        )
    )
    client = AsyncOpenAI(api_key=api_key)
    findings: List[Dict[str, str]] = []

    for locale_code in sorted({change.locale_code for change in changes if change.locale_code}):
        locale_changes = [change for change in changes if change.locale_code == locale_code]
        findings.extend(
            await review_translation_changes(
                client=client,
                model=model,
                target_language=language_names.get(locale_code, locale_code),
                changes=locale_changes,
                style_rules=style_rules_by_locale.get(locale_code, []),
                brand_glossary=brand_glossary,
            )
        )

    append_semantic_review_findings(args.validation_summary, findings)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    return asyncio.run(_run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
