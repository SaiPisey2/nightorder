from bosun.teams import build_adaptive_card, _display_name


def test_display_name():
    assert _display_name("jane.doe@example.com") == "Jane Doe"
    assert _display_name("jdoe@x.com") == "Jdoe"


def test_card_with_mentions_buttons_facts():
    msg = build_adaptive_card(
        title="Approval required — budget_approval",
        body="Cost 31000 USD exceeds 27000 USD.",
        facts={"project": "batch", "step": "cost-approval"},
        buttons=[{"title": "Approve", "url": "http://x/approve"},
                 {"title": "Reject", "url": "http://x/reject"}],
        mentions=["jane.doe@example.com"],
    )
    assert msg["type"] == "message"
    card = msg["attachments"][0]["content"]
    assert msg["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    # mention entity present and referenced in body text
    entities = card["msteams"]["entities"]
    assert entities == [{
        "type": "mention", "text": "<at>Jane Doe</at>",
        "mentioned": {"id": "jane.doe@example.com", "name": "Jane Doe"},
    }]
    rendered = str(card["body"])
    assert "<at>Jane Doe</at>" in rendered
    # buttons + facts
    assert [a["title"] for a in card["actions"]] == ["Approve", "Reject"]
    facts = next(b for b in card["body"] if b["type"] == "FactSet")["facts"]
    assert {"title": "project", "value": "batch"} in facts


def test_card_without_mentions_has_no_entities():
    msg = build_adaptive_card(title="t", body="b")
    assert "msteams" not in msg["attachments"][0]["content"]
    assert "actions" not in msg["attachments"][0]["content"]
