#!/usr/bin/env python3
"""Generate deliberately messy trade quote PDFs for Extractor testing.

Real regional quotes are inconsistent: different labels for the total, missing
customer phone numbers, mixed currency notation, tax handled three ways, dates
in ambiguous formats. Each fixture below exercises a specific failure mode, and
the expected extraction is recorded alongside in fixtures/quotes/expected.json.

    python scripts/generate_test_quotes.py    # requires reportlab
"""
import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "fixtures" / "quotes"


def _line(c, x, y, text, font="Helvetica", size=10, fill=colors.black):
    c.setFont(font, size)
    c.setFillColor(fill)
    c.drawString(x, y, text)


def uk_fencing(path):
    """Tidy-ish UK quote. Has phone. VAT shown separately. Total labelled 'TOTAL DUE'."""
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    _line(c, 20 * mm, h - 25 * mm, "HENDERSON FENCING LTD", "Helvetica-Bold", 16)
    _line(c, 20 * mm, h - 32 * mm, "Unit 4 Brightwell Ind Est, Suffolk IP10 0BJ  |  01473 555 214")
    _line(c, 20 * mm, h - 37 * mm, "VAT Reg 384 9912 07")
    c.setStrokeColor(colors.grey)
    c.line(20 * mm, h - 42 * mm, w - 20 * mm, h - 42 * mm)

    _line(c, 20 * mm, h - 55 * mm, "QUOTATION", "Helvetica-Bold", 13)
    _line(c, 20 * mm, h - 63 * mm, "Ref: HF-1042            Date: 11/09/2026")
    _line(c, 20 * mm, h - 70 * mm, "For the attention of: Mark Henderson")
    _line(c, 20 * mm, h - 76 * mm, "Sutton Hall Farm, Woodbridge")
    _line(c, 20 * mm, h - 82 * mm, "Mob: 07700 900412")

    _line(c, 20 * mm, h - 96 * mm, "Description", "Helvetica-Bold")
    _line(c, 150 * mm, h - 96 * mm, "Amount", "Helvetica-Bold")
    rows = [
        ("Supply & install 2km stock fencing, HT8/80/15", "14,200.00"),
        ("Strainer posts & bracing (28 no.)", "3,640.00"),
        ("Gates - 2 x 12ft galv field gate incl hanging", "1,180.00"),
        ("Site clearance & existing fence removal", "1,400.00"),
    ]
    y = h - 104 * mm
    for desc, amt in rows:
        _line(c, 20 * mm, y, desc)
        _line(c, 150 * mm, y, amt)
        y -= 7 * mm

    y -= 4 * mm
    _line(c, 120 * mm, y, "Subtotal"); _line(c, 150 * mm, y, "20,420.00"); y -= 6 * mm
    _line(c, 120 * mm, y, "VAT @ 20%"); _line(c, 150 * mm, y, "4,084.00"); y -= 8 * mm
    _line(c, 120 * mm, y, "TOTAL DUE", "Helvetica-Bold", 11)
    _line(c, 150 * mm, y, "GBP 24,504.00", "Helvetica-Bold", 11)

    y -= 20 * mm
    _line(c, 20 * mm, y, "Quotation valid 30 days from date of issue.", size=8.5, fill=colors.grey)
    _line(c, 20 * mm, y - 5 * mm, "Payment 30% deposit, balance on completion. Subject to ground conditions.",
          size=8.5, fill=colors.grey)
    c.save()


def au_shed(path):
    """AU shed builder. NO customer phone anywhere. GST inclusive. Total labelled 'INVESTMENT'."""
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    c.setFillColor(colors.HexColor("#1F3A5F"))
    c.rect(0, h - 30 * mm, w, 30 * mm, stroke=0, fill=1)
    _line(c, 18 * mm, h - 19 * mm, "RIVERINA RURAL STRUCTURES", "Helvetica-Bold", 15, colors.white)
    _line(c, 18 * mm, h - 25 * mm, "ABN 44 128 991 023   |   Wagga Wagga NSW", size=9, fill=colors.white)

    _line(c, 18 * mm, h - 40 * mm, "Quote QR-2291        Issued 11 September 2026", size=9)
    _line(c, 18 * mm, h - 45 * mm, "Proposal prepared for", "Helvetica-Bold", 11)
    _line(c, 18 * mm, h - 53 * mm, "Denise Okafor")
    _line(c, 18 * mm, h - 59 * mm, "'Barrenjoey', Old Narrandera Rd")
    _line(c, 18 * mm, h - 65 * mm, "denise.okafor@barrenjoeypastoral.com.au")

    _line(c, 18 * mm, h - 80 * mm, "Scope of works", "Helvetica-Bold", 11)
    body = [
        "18m x 24m x 5.5m machinery shed, 6 bay, open one side.",
        "Colorbond cladding (Woodland Grey), gutter and downpipe to tank.",
        "Engineered slab 150mm, mesh SL82, includes pour and finish.",
        "Council engineering certification included. Excludes site cut and",
        "any rock excavation should it be encountered.",
    ]
    y = h - 88 * mm
    for ln in body:
        _line(c, 18 * mm, y, ln); y -= 6 * mm

    y -= 10 * mm
    _line(c, 18 * mm, y, "Stage 1  Deposit on acceptance", ); _line(c, 140 * mm, y, "$18,500"); y -= 7 * mm
    _line(c, 18 * mm, y, "Stage 2  Slab poured"); _line(c, 140 * mm, y, "$26,000"); y -= 7 * mm
    _line(c, 18 * mm, y, "Stage 3  Frame erected"); _line(c, 140 * mm, y, "$22,000"); y -= 7 * mm
    _line(c, 18 * mm, y, "Stage 4  Practical completion"); _line(c, 140 * mm, y, "$16,340"); y -= 12 * mm

    _line(c, 18 * mm, y, "TOTAL INVESTMENT (inc GST)", "Helvetica-Bold", 12)
    _line(c, 140 * mm, y, "$82,840", "Helvetica-Bold", 12)
    y -= 18 * mm
    _line(c, 18 * mm, y, "Pricing held for 14 days. Steel subject to mill variation thereafter.",
          size=8.5, fill=colors.grey)
    c.save()


def ie_groundworks(path):
    """Irish groundworks. Prose, no table. EUR with symbol. Date written long-form. Two figures."""
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    _line(c, 22 * mm, h - 28 * mm, "T. Brennan Plant & Groundworks", "Helvetica-Bold", 14)
    _line(c, 22 * mm, h - 35 * mm, "Ballinasloe, Co. Galway     086 7729140     tbrennanplant@eircom.net", size=9)

    _line(c, 22 * mm, h - 50 * mm, "14th September 2026")
    _line(c, 22 * mm, h - 60 * mm, "Padraig Ui Chonaill")
    _line(c, 22 * mm, h - 66 * mm, "Clonfert Road")

    para = [
        "Padraig,",
        "",
        "Further to walking the site on Tuesday, please find below my price for the entrance",
        "and yard works discussed.",
        "",
        "Strip and cart away topsoil to spoil heap on site, form new entrance off the county",
        "road including sightlines both directions, supply and lay 804 to 300mm compacted in",
        "two layers, install 2no. 300mm dia culverts with headwalls, and surface dress the",
        "yard area of approximately 640 square metres.",
        "",
        "Price for the above works, ex VAT:            EUR 31,750",
        "",
        "Should you wish to include the tarmac finish to the entrance apron as we spoke about,",
        "add a further EUR 4,900 to the above.",
        "",
        "I would be in a position to start the first week of October if that suits. This price",
        "holds to end of the month.",
        "",
        "Le meas,",
        "Tom Brennan",
    ]
    y = h - 80 * mm
    for ln in para:
        _line(c, 22 * mm, y, ln)
        y -= 5.6 * mm
    c.save()


EXPECTED = {
    "uk_fencing.pdf": {
        "note": "Clean case. Phone present. Total labelled 'TOTAL DUE'. VAT separate - extract gross.",
        "customer_name": "Mark Henderson", "customer_phone": "07700 900412",
        "project_title": "2km stock fencing", "quote_total": 24504.00, "currency": "GBP",
        "expiry_date": "2026-10-11", "traps": ["subtotal vs total", "VAT line could be mistaken for total"],
    },
    "au_shed.pdf": {
        "note": "No phone anywhere - extractor MUST return null, not hallucinate. Email only.",
        "customer_name": "Denise Okafor", "customer_phone": None,
        "project_title": "18m x 24m machinery shed", "quote_total": 82840.00, "currency": "AUD",
        "expiry_date": "2026-09-25",  # issued 11 Sep + "held for 14 days"
        "traps": ["no phone", "4 stage payments could be mistaken for total", "'INVESTMENT' not 'total'"],
    },
    "ie_groundworks.pdf": {
        "note": "Prose, no table. Two figures - base price is the quote, the tarmac add-on is optional.",
        "customer_name": "Padraig Ui Chonaill", "customer_phone": None,
        "project_title": "Entrance and yard groundworks", "quote_total": 31750.00, "currency": "EUR",
        "expiry_date": "2026-09-30",
        "traps": ["optional extra must NOT be added to total", "ex-VAT", "'end of the month' expiry"],
    },
}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    uk_fencing(OUT / "uk_fencing.pdf")
    au_shed(OUT / "au_shed.pdf")
    ie_groundworks(OUT / "ie_groundworks.pdf")
    (OUT / "expected.json").write_text(json.dumps(EXPECTED, indent=2) + "\n")
    for f in sorted(OUT.iterdir()):
        print(f"  {f.name}")
