from fastmcp import FastMCP
from mcp.server.fastmcp import Icon
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from tools import (
    get_document_by_number,
    get_document_text,
    get_recent_documents,
    get_vorgang,
    search_documents,
)

####### SERVER #######

icon = Icon(
    src="https://raw.githubusercontent.com/SimonBerg255/landtag-mv-mcp/main/icon.png",
)

INSTRUCTION_STRING = """
Server für parlamentarische Recherche im Landtag Mecklenburg-Vorpommern.
Alle Inhalte sind auf Deutsch. Suchbegriffe IMMER auf Deutsch formulieren.
Datenquelle: Parlamentsdatenbank (ParlDok) — https://www.dokumentation.landtag-mv.de/parldok

ENTSCHEIDUNGSBAUM — wähle das richtige Tool:

1. NUTZER FRAGT NACH THEMA / SUCHT DOKUMENTE:
   → search_documents — durchsucht die gesamte Parlamentsdatenbank
   Beispiele: "Was gibt es zu Windenergie?", "Kleine Anfragen zum Thema Schule"
   WORKFLOW: search_documents → get_document_text für Volltext

2. NUTZER KENNT EINE DOKUMENTNUMMER (z. B. "8/5012"):
   → get_document_by_number — findet das exakte Dokument
   WORKFLOW: get_document_by_number → get_document_text für Volltext

3. NUTZER WILL VOLLTEXT EINES DOKUMENTS LESEN:
   → get_document_text(document_id=<ID>) — extrahiert Text aus dem PDF
   KONTEXT-LIMIT: Text wird auf max. 20.000 Zeichen begrenzt.
   Der PDF-Link wird immer mitgeliefert für den vollständigen Text.

4. NUTZER FRAGT NACH EINEM VORGANG (parlamentarisches Verfahren):
   → get_vorgang(vorgang_id=<ID>) — zeigt alle zugehörigen Dokumente
   Ein Vorgang bündelt z. B. eine Anfrage mit der Antwort.

5. NUTZER WILL NEUESTE DOKUMENTE SEHEN:
   → get_recent_documents — die aktuellsten Veröffentlichungen
   Optional filterbar nach Dokumentart (Drucksache, Plenarprotokoll, etc.)

WICHTIGE HINTERGRUNDINFORMATIONEN:
- Wahlperiode 8 = aktuell (seit 2021), WP 1–7 = historisch
- Dokumentarten: Drucksache, Plenarprotokoll, Amtliche Mitteilung, Beschlussprotokoll
- Eine Drucksache kann sein: Kleine Anfrage, Große Anfrage, Gesetzentwurf, Antrag, Antwort, Bericht
- Datumsformat: DD.MM.YYYY (z. B. "01.01.2024")
- Fraktionen: SPD, CDU, AfD, DIE LINKE, BÜNDNIS 90/DIE GRÜNEN, FDP
- PDFs sind öffentlich zugänglich ohne Authentifizierung
"""

mcp = FastMCP(
    name="Landtag Mecklenburg-Vorpommern — Parlamentsdatenbank",
    instructions=INSTRUCTION_STRING,
    version="1.0.0",
    website_url="https://www.dokumentation.landtag-mv.de/parldok",
    icons=[icon],
)

####### TOOLS #######

mcp.tool(meta={"requires_permission": False})(search_documents)
mcp.tool(meta={"requires_permission": False})(get_document_by_number)
mcp.tool(meta={"requires_permission": False})(get_document_text)
mcp.tool(meta={"requires_permission": False})(get_vorgang)
mcp.tool(meta={"requires_permission": False})(get_recent_documents)

####### ROUTES #######


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


####### APP #######
# Run with: uvicorn server:app --host 0.0.0.0 --port $PORT

app = mcp.http_app()
