from api.docparse.finalize import render_final_markdown


def test_render_final_markdown_keeps_structure():
    blocks = [
        {"type": "header", "text": "页眉", "page_num": 1},
        {"type": "text", "text": "第一段", "heading_level": 1, "page_num": 1},
        {"type": "text", "text": "正文", "page_num": 1},
        {
            "type": "table",
            "text": "| a | b |\n|---|---|\n| 1 | 2 |",
            "page_num": 1,
        },
        {"type": "footer", "text": "页脚", "page_num": 1},
    ]
    rendered = render_final_markdown(blocks)
    assert "# 第一段" in rendered
    assert "正文" in rendered
    assert "| a | b |" in rendered
    assert "页眉" not in rendered
    assert "页脚" not in rendered
