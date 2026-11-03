"""Teksten voor outreach: met Claude (indien sleutel) of met een vast sjabloon.

De verplichte onderdelen (wie we zijn, waar we de gegevens vonden, uitschrijven)
worden altijd door de code toegevoegd, nooit aan het model overgelaten.
"""
from __future__ import annotations

import json

from .score import BRIEFING

SECTOR_HOOK = {
    "horeca": "sfeer, gerechten en team in korte Reels die mensen doen reserveren",
    "fitness": "coaches, lessen en community in herkenbare video's die nieuwe leden aantrekken",
    "lifestyle": "collecties en lanceringen in een vaste stroom productvideo's",
}

EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        k: {"type": "object",
            "properties": {"onderwerp": {"type": "string"}, "tekst": {"type": "string"}},
            "required": ["onderwerp", "tekst"], "additionalProperties": False}
        for k in ("mail", "opvolging_1", "opvolging_2")
    },
    "required": ["mail", "opvolging_1", "opvolging_2"],
    "additionalProperties": False,
}
DM_SCHEMA = {"type": "object", "properties": {"dm": {"type": "string"}},
             "required": ["dm"], "additionalProperties": False}

WRITER_SYSTEM = f"""Je schrijft korte, persoonlijke cold outreach voor The Format House.

{BRIEFING}
Regels:
- Nederlands; Vlaamse toon voor Belgische bedrijven (bv. 'jullie', 'graag', 'even'), niet stijf.
- Gebruik UITSLUITEND de meegegeven data. Verzin geen complimenten, cijfers, campagnes,
  namen of feiten. Spreek het team aan ('Dag team van …'), nooit een persoon bij naam.
- Concreet en kort: de eerste mail max. ~110 woorden, opvolgmails max. ~60 woorden.
- Leg uit waarom we hen contacteren (op basis van de salesinsteek en signalen).
- Eén duidelijke, laagdrempelige vraag (bv. 15 minuten kennismaken of voorbeelden sturen).
- Geen afsluitende groet of handtekening en geen uitschrijftekst: die voegt de software toe.
"""


def _lead_brief(lead: dict) -> str:
    return json.dumps(lead, ensure_ascii=False)


def _claude_json(client, model: str, prompt: str, schema: dict) -> dict:
    resp = client.beta.messages.create(
        model=model, max_tokens=8000, system=WRITER_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    )
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"geen bruikbare tekst (stop_reason={resp.stop_reason})")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


# ── E-mail ───────────────────────────────────────────────────────────

def email_footer(*, email: str, source_url: str, unsubscribe: str) -> str:
    unsub = f"of mail naar {unsubscribe} " if unsubscribe and unsubscribe != email else ""
    return (
        "\n\n—\n"
        "The Format House is een Belgisch creatief content agency voor professionele "
        "short-form video (Instagram Reels, TikTok, YouTube Shorts).\n"
        f"We vonden {email} als algemeen contactadres op {source_url}.\n"
        f"Liever geen berichten meer van ons? Antwoord met 'uitschrijven' {unsub}"
        "en we contacteren jullie niet opnieuw."
    )


def template_emails(lead: dict) -> dict:
    name, city = lead["bedrijf"], lead.get("stad") or "jullie regio"
    hook = SECTOR_HOOK.get(lead.get("sectorgroep"), "jullie aanbod in een vaste stroom short-form video")
    insteek = lead.get("salesinsteek") or ""
    return {
        "mail": {
            "onderwerp": f"Short-form video voor {name}?",
            "tekst": (
                f"Dag team van {name},\n\n"
                f"Bij The Format House maken we professionele Reels, TikToks en Shorts voor "
                f"bedrijven die social serieus nemen, maar niet elke week zelf de tijd hebben om "
                f"te filmen en te monteren. Voor zaken zoals die van jullie in {city} denken we "
                f"aan {hook}.\n\n"
                + (f"{insteek}\n\n" if insteek else "")
                + "Zin in een korte kennismaking van 15 minuten? Dan tonen we graag een paar "
                "voorbeelden uit jullie sector."
            ),
        },
        "opvolging_1": {
            "onderwerp": f"Re: Short-form video voor {name}?",
            "tekst": (f"Dag team van {name},\n\nEven een korte opvolging van mijn vorige mail. "
                      "Mag ik jullie twee of drie voorbeelden doorsturen van wat we voor "
                      "vergelijkbare zaken maken? Dan zien jullie meteen of het past."),
        },
        "opvolging_2": {
            "onderwerp": f"Re: Short-form video voor {name}?",
            "tekst": (f"Dag team van {name},\n\nIk wil jullie niet blijven storen, dus dit is "
                      "mijn laatste bericht hierover. Als short-form video later wel op de "
                      "agenda komt, dan horen we het graag."),
        },
    }


def write_emails(lead: dict, *, client=None, model: str = "") -> tuple[dict, str]:
    if client is not None:
        prompt = ("Schrijf een eerste cold mail en twee opvolgmails (na 4 en 9 dagen, "
                  "die enkel verstuurd worden als er geen antwoord kwam) voor deze lead:\n"
                  + _lead_brief(lead))
        return _claude_json(client, model, prompt, EMAIL_SCHEMA), f"claude:{model}"
    return template_emails(lead), "sjabloon"


# ── Instagram-DM ─────────────────────────────────────────────────────

DM_MAX = 400


def template_dm(lead: dict) -> str:
    hook = SECTOR_HOOK.get(lead.get("sectorgroep"), "jullie aanbod in een vaste stroom korte video's")
    dm = (f"Hoi team van {lead['bedrijf']}! Ik ben van The Format House, een Belgisch content "
          f"agency voor Reels en TikTok. We zien veel potentieel om {hook} te brengen. "
          "Mag ik jullie een paar voorbeelden sturen? Vrijblijvend uiteraard.")
    return dm[:DM_MAX]


def write_dm(lead: dict, *, client=None, model: str = "") -> tuple[str, str]:
    if client is not None:
        prompt = (f"Schrijf één korte Instagram-DM (max. {DM_MAX - 40} tekens, geen hashtags, "
                  "hoogstens één emoji, geen handtekening) voor deze lead:\n" + _lead_brief(lead))
        dm = _claude_json(client, model, prompt, DM_SCHEMA)["dm"].strip()
        if len(dm) <= DM_MAX:
            return dm, f"claude:{model}"
    return template_dm(lead), "sjabloon"
