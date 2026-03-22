"""
Tools for Landtag Mecklenburg-Vorpommern Parlamentsdatenbank MCP Server.

Data source: ParlDok 8.3.5 at https://www.dokumentation.landtag-mv.de/parldok
No official API — reverse-engineered JSON endpoints (2026-03-22).

API architecture:
  - All endpoints use HTTP POST with Content-Type: application/x-www-form-urlencoded
  - Body is always: data=<JSON string>
  - Response: {"success": true, "data": "<double-encoded JSON string>"}
  - The `data` field must be parsed TWICE (JSON within JSON)
  - No authentication or cookies required for public read endpoints

Key endpoints:
  POST /parldok/Fulltext/Search   — main search with tag-based filters
  POST /parldok/Fulltext/Resultpage — pagination within existing search
  POST /parldok/Document/Details  — single document metadata
  GET  /parldok/vorgang/{id}      — Vorgang page (HTML, parse data-pd-process attr)
  GET  /parldok/dokument/{id}     — direct PDF download

Search tag system (type constants):
  0  = Volltext (fulltext search, text in "fulltext" field)
  2  = Fraktionen (parliamentary groups)
  7  = Dokumentart: 1=Drucksache, 2=Plenarprotokoll, 6=Amtliche Mitteilung, 12=Beschlussprotokoll
  9  = Zeitraum (date range, field: "datefrom"/"dateto"/"both", format DD.MM.YYYY)
  10 = Wahlperiode (legislative period, e.g. "8")
  14 = Nummer (document number)

Tags of different types are AND-combined; same type with ored=true are OR-combined.
"""

import asyncio
import html as html_module
import io
import json
import re
from typing import Optional

import httpx
import pdfplumber

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.dokumentation.landtag-mv.de/parldok"
USER_AGENT = "Mozilla/5.0 (compatible; LandtagMV-MCP/1.0; parliamentary research)"

MAX_TEXT_CHARS = 20_000
MAX_RESPONSE_CHARS = 24_000
REQUEST_DELAY = 0.5  # seconds between requests

# Document type mapping for the tag system (type 7)
DOKUMENTART_IDS = {
    "Drucksache": "1",
    "Plenarprotokoll": "2",
    "Ausschussprotokoll": "3",
    "Gesetz- und Verordnungsblatt": "4",
    "Kommissionsdrucksache": "5",
    "Amtliche Mitteilung": "6",
    "Kommissionsprotokoll": "7",
    "Beschlussprotokoll": "12",
}

# Shared async HTTP client
_client: httpx.AsyncClient | None = None

# Simple in-memory cache for PDF text
_pdf_text_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    return _client


async def _post_api(endpoint: str, payload: dict) -> dict:
    """POST to a ParlDok API endpoint and return the parsed data."""
    client = _get_client()
    url = f"{BASE_URL}/{endpoint}"
    resp = await client.post(
        url,
        data={"data": json.dumps(payload)},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    outer = resp.json()
    if not outer.get("success"):
        raise ValueError(f"API error: {outer.get('message', 'unknown')}")
    # Double-decode: data field is a JSON string
    inner = json.loads(outer["data"])
    return inner


async def _delay():
    await asyncio.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# Tag builder helpers
# ---------------------------------------------------------------------------


def _tag(type_: int, id_: str = "", fulltext: str = "", field: str = "", label: str = "") -> dict:
    return {
        "type": type_,
        "id": id_,
        "ored": True,
        "field": field,
        "fulltext": fulltext,
        "label": label,
    }


def _build_search_tags(
    query: str | None = None,
    document_type: str | None = None,
    wahlperiode: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    fraktion: str | None = None,
) -> list[dict]:
    """Build tag array for Fulltext/Search from user parameters."""
    tags = []

    if query:
        tags.append(_tag(0, fulltext=query, label=query))

    if document_type:
        doc_id = DOKUMENTART_IDS.get(document_type, "")
        if doc_id:
            tags.append(_tag(7, id_=doc_id, label=document_type))

    if wahlperiode is not None:
        tags.append(_tag(10, id_=str(wahlperiode), label=f"WP {wahlperiode}"))

    if date_from:
        tags.append(_tag(9, id_=date_from, field="datefrom", label=f"ab {date_from}"))

    if date_to:
        tags.append(_tag(9, id_=date_to, field="dateto", label=f"bis {date_to}"))

    # Fraktion: use type 2 — we pass the name as label, id needs lookup
    # Since we don't have a static ID map, we use the search facet approach
    # For now, if fraktion is provided we add a fulltext filter scoped to it
    if fraktion:
        tags.append(_tag(2, id_="", label=fraktion, fulltext=fraktion))

    return tags


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _fmt_doc(doc: dict, index: int) -> list[str]:
    """Format a document from search results into display lines."""
    wp = doc.get("lp", "?")
    number = doc.get("number", "?")
    doc_number = f"{wp}/{number}" if number else "N/A"
    doc_id = doc.get("id", "?")
    pdf_url = f"{BASE_URL}/dokument/{doc_id}" if doc_id else "N/A"

    lines = [
        f"[{index}] {doc.get('kind', 'Dokument')} — {doc.get('type', '')}",
        f"    Nummer: {doc_number} | ID: {doc_id}",
        f"    Titel: {doc.get('title', 'N/A')}",
        f"    Datum: {doc.get('date', 'N/A')} | Wahlperiode: {wp}",
        f"    PDF: {pdf_url}",
    ]

    if doc.get("authorhtml"):
        # Strip HTML tags from author
        author = re.sub(r"<[^>]+>", "", doc["authorhtml"]).strip()
        if author:
            lines.append(f"    Urheber: {author}")

    if doc.get("processid") and doc["processid"] > 0:
        lines.append(f"    Vorgang-ID: {doc['processid']}")

    return lines


def _truncate(output: str) -> str:
    if len(output) > MAX_RESPONSE_CHARS:
        return output[:MAX_RESPONSE_CHARS] + f"\n\n[Ausgabe auf {MAX_RESPONSE_CHARS:,} Zeichen begrenzt]"
    return output


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def search_documents(
    query: Optional[str] = None,
    document_type: Optional[str] = None,
    wahlperiode: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    fraktion: Optional[str] = None,
    limit: int = 20,
) -> str:
    """
    Durchsucht die Parlamentsdatenbank des Landtags Mecklenburg-Vorpommern.

    Findet Drucksachen, Plenarprotokolle, Amtliche Mitteilungen und andere
    parlamentarische Dokumente. Alle Inhalte sind auf Deutsch.

    Typische document_type-Werte:
      "Drucksache"           — Gesetzentwürfe, Anfragen, Anträge, Berichte, Antworten
      "Plenarprotokoll"      — Wortprotokolle der Plenarsitzungen
      "Amtliche Mitteilung"  — Offizielle Mitteilungen
      "Beschlussprotokoll"   — Beschlussprotokolle

    Datumsformat: DD.MM.YYYY (z. B. "01.01.2024")

    Args:
        query: Suchbegriff auf Deutsch (z. B. "Windenergie", "Schule", "Haushalt")
        document_type: Dokumentart (z. B. "Drucksache", "Plenarprotokoll")
        wahlperiode: Legislaturperiode als Zahl (1–8, aktuell: 8)
        date_from: Startdatum DD.MM.YYYY
        date_to: Enddatum DD.MM.YYYY
        fraktion: Fraktion (z. B. "SPD", "CDU", "AfD", "DIE LINKE")
        limit: Maximale Anzahl Ergebnisse (Standard: 20, max: 50)

    Returns:
        Liste der gefundenen Dokumente mit Nummer, Titel, Typ, Datum und PDF-Link.
        Die ID kann für get_document_text verwendet werden.
    """
    try:
        limit = min(max(limit, 1), 50)
        tags = _build_search_tags(query, document_type, wahlperiode, date_from, date_to, fraktion)

        payload = {
            "devicekey": "",
            "max": 1000,
            "withfilter": False,
            "sort": 2,  # Datum desc (newest first)
            "topk": 3,
            "llm": 0,
            "newdocsearch": False,
            "limit": {"Start": 0, "Length": limit},
            "tags": tags,
        }

        data = await _post_api("Fulltext/Search", payload)
        docs = data.get("docs", [])
        count = data.get("count", 0)

        if not docs:
            return "Keine Dokumente für die angegebenen Kriterien gefunden."

        lines = [
            f"PARLAMENTSDATENBANK LANDTAG M-V — {count:,} Treffer, zeige {len(docs)}",
            "=" * 60,
        ]
        for i, doc in enumerate(docs, 1):
            lines.extend(_fmt_doc(doc, i))
            lines.append("")

        if count > len(docs):
            lines.append(f"Weitere {count - len(docs):,} Dokumente verfügbar — Suche eingrenzen oder limit erhöhen.")

        lines.append("")
        lines.append("NÄCHSTE SCHRITTE:")
        lines.append("→ Volltext lesen: get_document_text(document_id=<ID>)")
        lines.append("→ Vorgang anzeigen: get_vorgang(vorgang_id=<Vorgang-ID>)")

        return _truncate("\n".join(lines))
    except Exception as e:
        return json.dumps({"error": str(e)})


async def get_document_by_number(document_number: str) -> str:
    """
    Ruft ein Dokument anhand seiner parlamentarischen Nummer ab (z. B. "8/5012").

    Die Nummer hat das Format "Wahlperiode/Nummer" (z. B. "8/5012", "7/1234").
    Gibt vollständige Metadaten inklusive PDF-URL und Vorgang-Informationen zurück.

    Args:
        document_number: Parlamentarische Dokumentnummer (z. B. "8/5012", "7/3456")

    Returns:
        Metadaten des Dokuments mit Titel, Typ, Datum, PDF-Link und Vorgang-ID.
    """
    try:
        # Parse the document number
        parts = document_number.strip().split("/")
        if len(parts) != 2:
            return json.dumps({"error": f"Ungültiges Nummernformat: '{document_number}'. Erwartet: 'WP/Nummer' (z. B. '8/5012')"})

        wp = parts[0].strip()
        nummer = parts[1].strip()

        # Search with Wahlperiode tag + Nummer tag
        tags = [
            _tag(10, id_=wp, label=f"WP {wp}"),
        ]

        # Use fulltext search scoped to title for the number
        # Actually, we search with the document number directly
        payload = {
            "devicekey": "",
            "max": 100,
            "withfilter": False,
            "sort": 0,
            "topk": 3,
            "llm": 0,
            "newdocsearch": False,
            "limit": {"Start": 0, "Length": 20},
            "tags": tags + [_tag(0, fulltext=nummer, label=nummer)],
        }

        data = await _post_api("Fulltext/Search", payload)
        docs = data.get("docs", [])

        # Find exact match
        target = None
        for doc in docs:
            doc_wp = str(doc.get("lp", ""))
            doc_num = str(doc.get("number", ""))
            if doc_wp == wp and doc_num == nummer:
                target = doc
                break

        if not target:
            # Try broader search without fulltext, just WP + iterate
            return f"Dokument {document_number} nicht gefunden. Versuchen Sie search_documents mit query=\"{nummer}\" und wahlperiode={wp}."

        doc_id = target.get("id", "?")
        doc_number = f"{target.get('lp', '?')}/{target.get('number', '?')}"
        pdf_url = f"{BASE_URL}/dokument/{doc_id}"

        author = ""
        if target.get("authorhtml"):
            author = re.sub(r"<[^>]+>", "", target["authorhtml"]).strip()

        lines = [
            "DOKUMENT — DETAILS",
            "=" * 60,
            f"ID:           {doc_id}",
            f"Nummer:       {doc_number}",
            f"Dokumentart:  {target.get('kind', 'N/A')}",
            f"Dokumenttyp:  {target.get('type', 'N/A')}",
            f"Titel:        {target.get('title', 'N/A')}",
            f"Datum:        {target.get('date', 'N/A')}",
            f"Wahlperiode:  {target.get('lp', 'N/A')}",
            f"PDF:          {pdf_url}",
        ]

        if author:
            lines.append(f"Urheber:      {author}")

        if target.get("processid") and target["processid"] > 0:
            lines.append(f"Vorgang-ID:   {target['processid']}")
            lines.append("")
            lines.append("NÄCHSTE SCHRITTE:")
            lines.append(f"→ Volltext: get_document_text(document_id='{doc_id}')")
            lines.append(f"→ Vorgang: get_vorgang(vorgang_id={target['processid']})")
        else:
            lines.append("")
            lines.append("NÄCHSTE SCHRITTE:")
            lines.append(f"→ Volltext: get_document_text(document_id='{doc_id}')")

        return "\n".join(lines)
    except Exception as e:
        return json.dumps({"error": str(e)})


async def get_document_text(
    document_id: str,
    document_number: Optional[str] = None,
) -> str:
    """
    Extrahiert den Volltext aus dem PDF eines parlamentarischen Dokuments.

    Lädt das PDF herunter und extrahiert den Text mit pdfplumber.
    Ergebnisse werden gecacht — PDFs ändern sich nicht nach Veröffentlichung.

    Der Text wird auf max. 20.000 Zeichen begrenzt. Bei längeren Dokumenten
    wird der PDF-Link für den vollständigen Text bereitgestellt.

    Args:
        document_id: Numerische Dokument-ID (z. B. "68623") — aus search_documents oder get_document_by_number
        document_number: Optionale Dokumentnummer für die Anzeige (z. B. "8/5012")

    Returns:
        Extrahierter Text mit Metadaten, Seitenanzahl und Quell-URL.
    """
    try:
        doc_id_str = str(document_id).strip()

        # Check cache
        if doc_id_str in _pdf_text_cache:
            cached = _pdf_text_cache[doc_id_str]
            text = cached["text"]
            is_truncated = len(text) > MAX_TEXT_CHARS
            display_text = text[:MAX_TEXT_CHARS]
            lines = [
                "DOKUMENTTEXT (aus Cache)",
                "=" * 60,
                f"Dokument-ID:  {doc_id_str}",
            ]
            if document_number:
                lines.append(f"Nummer:       {document_number}")
            lines += [
                f"Seiten:       {cached['pages']}",
                f"Zeichen:      {len(text):,}",
                f"PDF:          {cached['source_url']}",
                "",
                "TEXT:",
                "—" * 40,
                display_text,
            ]
            if is_truncated:
                lines.append(f"\n[… TEXT GEKÜRZT — noch {len(text) - MAX_TEXT_CHARS:,} Zeichen]")
                lines.append(f"Vollständiger Text als PDF: {cached['source_url']}")
            return _truncate("\n".join(lines))

        pdf_url = f"{BASE_URL}/dokument/{doc_id_str}"
        client = _get_client()

        resp = await client.get(pdf_url)
        if resp.status_code == 404:
            return json.dumps({"error": f"Dokument mit ID {doc_id_str} nicht gefunden."})
        resp.raise_for_status()

        # Check content type
        content_type = resp.headers.get("content-type", "")
        if "pdf" not in content_type.lower() and "octet-stream" not in content_type.lower():
            return json.dumps({"error": f"Unerwarteter Content-Type: {content_type}. Möglicherweise keine PDF-Datei."})

        # Extract text from PDF
        pdf_bytes = resp.content
        text_parts = []
        page_count = 0

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page_count = len(pdf.pages)
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)

        full_text = "\n\n".join(text_parts)

        if not full_text.strip():
            return json.dumps({
                "error": "Kein Text extrahierbar (möglicherweise gescanntes PDF ohne OCR).",
                "pdf_url": pdf_url,
                "pages": page_count,
            })

        # Cache the result
        _pdf_text_cache[doc_id_str] = {
            "text": full_text,
            "pages": page_count,
            "source_url": pdf_url,
        }

        is_truncated = len(full_text) > MAX_TEXT_CHARS
        display_text = full_text[:MAX_TEXT_CHARS]

        lines = [
            "DOKUMENTTEXT",
            "=" * 60,
            f"Dokument-ID:  {doc_id_str}",
        ]
        if document_number:
            lines.append(f"Nummer:       {document_number}")
        lines += [
            f"Seiten:       {page_count}",
            f"Zeichen:      {len(full_text):,}",
            f"PDF:          {pdf_url}",
            "",
            "TEXT:",
            "—" * 40,
            display_text,
        ]

        if is_truncated:
            lines.append(f"\n[… TEXT GEKÜRZT — noch {len(full_text) - MAX_TEXT_CHARS:,} Zeichen]")
            lines.append(f"Vollständiger Text als PDF: {pdf_url}")

        return _truncate("\n".join(lines))
    except Exception as e:
        return json.dumps({"error": str(e)})


async def get_vorgang(vorgang_id: int) -> str:
    """
    Ruft einen Vorgang (parlamentarisches Verfahren) mit allen zugehörigen Dokumenten ab.

    Ein Vorgang bündelt alle Dokumente eines parlamentarischen Prozesses — z. B.
    eine Kleine Anfrage mit der zugehörigen Antwort, oder ein Gesetzentwurf mit
    allen Lesungen und Ausschussberichten.

    Args:
        vorgang_id: Numerische Vorgang-ID (z. B. 12345) — aus search_documents oder get_document_by_number

    Returns:
        Vorgang-Details mit Titel, Typ, Status und Liste aller zugehörigen Dokumente.
    """
    try:
        client = _get_client()
        url = f"{BASE_URL}/vorgang/{vorgang_id}"
        resp = await client.get(url)

        if resp.status_code == 404:
            return json.dumps({"error": f"Vorgang {vorgang_id} nicht gefunden."})
        resp.raise_for_status()

        # Extract data-pd-process from HTML — value is HTML-entity-encoded JSON
        match = re.search(r'data-pd-process="(.*?)"', resp.text, re.DOTALL)
        if not match:
            return json.dumps({"error": "Keine Vorgang-Daten auf der Seite gefunden."})

        raw = html_module.unescape(match.group(1))
        process_data = json.loads(raw)

        title = process_data.get("title", "N/A")
        descriptor = process_data.get("descriptor", "")
        state = process_data.get("state", "N/A")
        wp = process_data.get("lp", "?")
        number = process_data.get("number", "")

        lines = [
            "VORGANG — DETAILS",
            "=" * 60,
            f"ID:           {vorgang_id}",
            f"Titel:        {title}",
            f"Deskriptor:   {descriptor}",
            f"Status:       {state}" if state else "",
            f"Wahlperiode:  {wp}",
            f"Nummer:       {number}",
        ]
        lines = [l for l in lines if l]  # remove empty lines

        if process_data.get("processabstract"):
            abstract = process_data["processabstract"][:500]
            lines += ["", "ZUSAMMENFASSUNG:", abstract]

        if process_data.get("transparencylink"):
            lines.append(f"Transparenz:  {process_data['transparencylink']}")

        # Extract documents from positions
        positions = process_data.get("positions", [])
        if positions:
            lines.append("")
            lines.append(f"ZUGEHÖRIGE DOKUMENTE ({len(positions)} Positionen):")
            lines.append("—" * 40)
            for i, pos in enumerate(positions, 1):
                pos_date = pos.get("date", "")
                pos_text = pos.get("text", "").replace("html:", "")
                doc = pos.get("doc")
                pages = pos.get("pages", "")
                comment = pos.get("comment", "")

                if doc:
                    doc_id = doc.get("id", "?")
                    doc_wp = doc.get("lp", "?")
                    doc_num = doc.get("number", "?")
                    doc_number = f"{doc_wp}/{doc_num}"
                    doc_title = doc.get("title", "N/A")
                    doc_kind = doc.get("kind", "")
                    doc_type = doc.get("type", "")
                    pdf_url = f"{BASE_URL}/dokument/{doc_id}"

                    lines.append(f"  [{i}] {doc_kind} — {doc_type}")
                    lines.append(f"      Nummer: {doc_number} | ID: {doc_id}")
                    lines.append(f"      Titel: {doc_title[:120]}")
                    lines.append(f"      Datum: {pos_date}{pages}")
                    lines.append(f"      PDF: {pdf_url}")
                    if comment:
                        lines.append(f"      Anmerkung: {comment}")
                else:
                    lines.append(f"  [{i}] {pos_date} — {pos_text}{pages}")
                    if comment:
                        lines.append(f"      Anmerkung: {comment}")
                lines.append("")

        lines.append("NÄCHSTE SCHRITTE:")
        lines.append("→ Volltext eines Dokuments: get_document_text(document_id=<ID>)")

        return _truncate("\n".join(lines))
    except Exception as e:
        return json.dumps({"error": str(e)})


async def get_recent_documents(
    document_type: Optional[str] = None,
    limit: int = 20,
) -> str:
    """
    Gibt die neuesten parlamentarischen Dokumente des Landtags M-V zurück.

    Nützlich zur Überwachung neuer Drucksachen, Plenarprotokolle und anderer
    Veröffentlichungen. Sortiert nach Datum (neueste zuerst).

    Typische document_type-Werte:
      "Drucksache"           — Anfragen, Anträge, Gesetzentwürfe, Antworten
      "Plenarprotokoll"      — Wortprotokolle der Plenarsitzungen
      "Amtliche Mitteilung"  — Offizielle Mitteilungen
      "Beschlussprotokoll"   — Beschlussprotokolle

    Args:
        document_type: Dokumentart filtern (optional)
        limit: Maximale Anzahl Ergebnisse (Standard: 20, max: 50)

    Returns:
        Liste der neuesten Dokumente mit Nummer, Titel, Typ, Datum und PDF-Link.
    """
    try:
        limit = min(max(limit, 1), 50)
        tags = []

        if document_type:
            doc_id = DOKUMENTART_IDS.get(document_type, "")
            if doc_id:
                tags.append(_tag(7, id_=doc_id, label=document_type))

        payload = {
            "devicekey": "",
            "max": 1000,
            "withfilter": False,
            "sort": 2,  # Datum desc (newest first)
            "topk": 3,
            "llm": 0,
            "newdocsearch": True,  # New documents mode
            "limit": {"Start": 0, "Length": limit},
            "tags": tags,
        }

        data = await _post_api("Fulltext/Search", payload)
        docs = data.get("docs", [])
        count = data.get("count", 0)

        if not docs:
            filter_text = f" vom Typ '{document_type}'" if document_type else ""
            return f"Keine aktuellen Dokumente{filter_text} gefunden."

        type_label = f" — {document_type}" if document_type else ""
        lines = [
            f"NEUESTE DOKUMENTE{type_label} — Landtag M-V",
            f"{count:,} Treffer, zeige {len(docs)}",
            "=" * 60,
        ]
        for i, doc in enumerate(docs, 1):
            lines.extend(_fmt_doc(doc, i))
            lines.append("")

        lines.append("NÄCHSTE SCHRITTE:")
        lines.append("→ Volltext lesen: get_document_text(document_id=<ID>)")
        lines.append("→ Vorgang anzeigen: get_vorgang(vorgang_id=<Vorgang-ID>)")

        return _truncate("\n".join(lines))
    except Exception as e:
        return json.dumps({"error": str(e)})
