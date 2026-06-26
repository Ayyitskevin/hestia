"""Marketing content — shot lists, captions, campaign copy (essence of Dionysus).

The research found Dionysus's "AI" was never wired in (deterministic templates,
no model). Here it's a real pluggable seam, same shape as vision/album:

- ``mock`` — deterministic, shoot-type-aware templates seeded by the project's
  vision keywords. No keys; the default; fully testable.
- ``xai`` — an LLM drafts the pack; defensive fallback to the template on failure.

A *pack* is generated from a project (and its galleries' vision keywords): a
headline, a strategy note, a shot list, and a set of social captions.
"""

from __future__ import annotations

import json
import sqlite3

from .config import Settings

# recipe slug → (label, applicable shoot types or None for all)
RECIPES = {
    "social-set": ("Social caption set", None),
    "shot-list": ("Pre-shoot shot list", None),
    "menu-launch": ("Menu launch pack", ("food",)),
    "brand-campaign": ("Brand campaign", ("commercial",)),
}


def recipes_for(shoot_type: str) -> list[dict]:
    out = []
    for slug, (label, applies) in RECIPES.items():
        if applies is None or shoot_type in applies:
            out.append({"slug": slug, "label": label})
    return out


_SHOT_LISTS = {
    "food": ["Overhead flat-lay of the hero dish", "45° hero with steam/garnish detail",
             "Ingredient close-up / texture macro", "Plating-in-progress action shot",
             "Table scene with hands + ambiance"],
    "commercial": ["Clean packshot on seamless", "In-context lifestyle hero",
                   "Detail / material macro", "Scale shot with a human element",
                   "Flat-lay of the product family"],
    "wedding": ["Getting-ready details (rings, dress)", "First-look reaction",
                "Ceremony wide + aisle moment", "Golden-hour couple portraits",
                "Reception candids + dance floor"],
    "_default": ["Establishing wide of the scene", "Hero subject portrait",
                 "Detail / texture close-up", "Candid in-the-moment frame",
                 "Closing atmosphere shot"],
}


def _caption_templates(name: str, kws: list[str]) -> list[str]:
    k = (kws + ["the moment", "every detail", "behind the scenes"])[:3]
    return [
        f"✨ {name}: where {k[0]} meets {k[1]}.",
        f"Every frame tells a story. Swipe for {k[2]}. 📸",
        f"Booked your date yet? {name} is filling up fast.",
        f"#{k[0].replace('-', '')} #{k[1].replace('-', '')} — captured, not staged.",
    ]


class MockContent:
    backend = "mock"

    def generate(self, *, project: dict, recipe: str, keywords: list[str]) -> dict:
        st = project.get("shoot_type", "other")
        name = project.get("name", "Your shoot")
        shot_list = _SHOT_LISTS.get(st, _SHOT_LISTS["_default"])
        headline = {
            "menu-launch": f"Introducing the new menu at {name}",
            "brand-campaign": f"{name}: built to be seen",
            "shot-list": f"Shot list — {name}",
        }.get(recipe, f"{name}, in frames worth sharing")
        strategy = (
            f"A {st} content set for {name}. Lead with the strongest hero frame, keep a "
            f"consistent edit, and post the caption set across the week. Keywords pulled "
            f"from the gallery: {', '.join(keywords[:6]) or 'n/a'}."
        )
        return {
            "headline": headline,
            "strategy": strategy,
            "shot_list": shot_list,
            "captions": _caption_templates(name, keywords),
        }


class XaiContent:
    backend = "xai"

    def __init__(self, settings: Settings):
        self.settings = settings

    def generate(self, *, project: dict, recipe: str, keywords: list[str]) -> dict:
        fallback = MockContent().generate(project=project, recipe=recipe, keywords=keywords)
        if not self.settings.xai_api_key:
            return fallback
        import httpx

        prompt = (
            f"Draft a {recipe} marketing pack for a {project.get('shoot_type')} photography "
            f"project '{project.get('name')}'. Gallery keywords: {', '.join(keywords) or 'none'}. "
            "Return ONLY JSON with keys: headline (string), strategy (string), "
            "shot_list (array of strings), captions (array of strings)."
        )
        try:
            with httpx.Client(base_url=self.settings.xai_base_url, timeout=60) as c:
                resp = c.post("/chat/completions", json={
                    "model": self.settings.xai_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                }, headers={"Authorization": f"Bearer {self.settings.xai_api_key}"})
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            body = json.loads(text[text.find("{"): text.rfind("}") + 1])
            # Validate shape; fall back per-field if the model under-delivered.
            return {
                "headline": str(body.get("headline") or fallback["headline"]),
                "strategy": str(body.get("strategy") or fallback["strategy"]),
                "shot_list": [str(x) for x in body.get("shot_list", [])] or fallback["shot_list"],
                "captions": [str(x) for x in body.get("captions", [])] or fallback["captions"],
            }
        except Exception:  # noqa: BLE001
            return fallback


def build_content(settings: Settings):
    if settings.content_backend == "xai":
        return XaiContent(settings)
    return MockContent()


# ── Keyword harvest + persistence ───────────────────────────────────────────


def project_keywords(conn: sqlite3.Connection, tenant_id: str, project_id: int, *, limit: int = 8) -> list[str]:
    rows = conn.execute(
        """
        SELECT ia.keywords_json FROM image_analyses ia
          JOIN galleries g ON g.id = ia.gallery_id AND g.tenant_id = ia.tenant_id
         WHERE g.project_id = ? AND ia.tenant_id = ?
        """,
        (project_id, tenant_id),
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        for kw in json.loads(r["keywords_json"] or "[]"):
            counts[kw] = counts.get(kw, 0) + 1
    return [kw for kw, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]]


def generate_pack(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tenant: dict,
    project: dict,
    recipe: str = "social-set",
    provider=None,
) -> dict:
    if recipe not in RECIPES:
        recipe = "social-set"
    provider = provider or build_content(settings)
    keywords = project_keywords(conn, tenant["id"], project["id"])
    body = provider.generate(project=project, recipe=recipe, keywords=keywords)
    label = RECIPES[recipe][0]
    cur = conn.execute(
        "INSERT INTO content_packs (tenant_id, project_id, title, recipe, backend, body_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant["id"], project["id"], f"{label} — {project['name']}", recipe,
         getattr(provider, "backend", "mock"), json.dumps(body)),
    )
    conn.commit()
    return get_pack(conn, tenant["id"], cur.lastrowid)


def list_packs(conn: sqlite3.Connection, tenant_id: str, *, project_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM content_packs WHERE tenant_id = ?"
    params: list = [tenant_id]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += " ORDER BY created_at DESC"
    return [_hydrate(dict(r)) for r in conn.execute(sql, params).fetchall()]


def get_pack(conn: sqlite3.Connection, tenant_id: str, pack_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM content_packs WHERE id = ? AND tenant_id = ?", (pack_id, tenant_id)
    ).fetchone()
    return _hydrate(dict(row)) if row else None


def approve_pack(conn: sqlite3.Connection, tenant_id: str, pack_id: int) -> None:
    conn.execute(
        "UPDATE content_packs SET status = 'approved', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (pack_id, tenant_id),
    )


def _hydrate(row: dict) -> dict:
    row["body"] = json.loads(row.pop("body_json") or "{}")
    return row
