from __future__ import annotations

from typing import Any, Dict, List

MD_HEADINGS = {
    "heading_1": "#",
    "heading_2": "##",
    "heading_3": "###",
}


def _extract_text_content(rich_text_list: List[Dict[str, Any]]) -> str:
    if not rich_text_list:
        return ""
    parts: List[str] = []
    for text_obj in rich_text_list:
        if text_obj.get("type") != "text":
            continue
        content = text_obj.get("text", {}).get("content", "")
        annotations = text_obj.get("annotations", {}) or {}
        if annotations.get("bold"):
            content = f"**{content}**"
        if annotations.get("italic"):
            content = f"*{content}*"
        if annotations.get("strikethrough"):
            content = f"~~{content}~~"
        if annotations.get("code"):
            content = f"`{content}`"
        parts.append(content)
    return "".join(parts)


def blocks_to_markdown(blocks: List[Dict[str, Any]]) -> str:
    """
    Convert a list of Notion blocks to markdown text. Only the block types that
    appear in our workspace are supported; the function fails safe by skipping
    unsupported blocks.
    """

    lines: List[str] = []
    for block in blocks or []:
        block_type = block.get("type")
        if block_type in MD_HEADINGS:
            text = _extract_text_content(block[block_type]["rich_text"])
            if text:
                lines.append(f"{MD_HEADINGS[block_type]} {text}\n")
            continue
        if block_type == "paragraph":
            text = _extract_text_content(block["paragraph"]["rich_text"])
            lines.append(f"{text}\n" if text else "\n")
            continue
        if block_type == "to_do":
            text = _extract_text_content(block["to_do"]["rich_text"])
            checked = block["to_do"].get("checked")
            checkbox = "[x]" if checked else "[ ]"
            if text:
                lines.append(f"- {checkbox} {text}\n")
            continue
        if block_type == "bulleted_list_item":
            text = _extract_text_content(block["bulleted_list_item"]["rich_text"])
            if text:
                lines.append(f"- {text}\n")
            continue
        if block_type == "numbered_list_item":
            text = _extract_text_content(block["numbered_list_item"]["rich_text"])
            if text:
                lines.append(f"1. {text}\n")
            continue
        if block_type == "code":
            text = _extract_text_content(block["code"]["rich_text"])
            language = block["code"].get("language", "")
            if text:
                lines.append(f"```{language}\n{text}\n```\n")
            continue
        if block_type == "quote":
            text = _extract_text_content(block["quote"]["rich_text"])
            if text:
                lines.append(f"> {text}\n")
            continue
        if block_type == "divider":
            lines.append("---\n")
            continue
    return "".join(lines)
