"""Render the sample purchase order used to test PO intake end to end.

    cd services/auditor && uv run python ../../samples/make_sample_po.py

Writes samples/wine-po-vine-and-dine.pdf. The lines mix exact buyer codes, fuzzy
descriptions and one item the cellar does not stock, so the draft shows every path:
exact match, vector match and "unmatched -> human review".
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body { font: 13px/1.45 Arial, sans-serif; color: #222; margin: 40px; }
h1 { font-size: 22px; margin: 0; } .muted { color: #666; }
table { width: 100%; border-collapse: collapse; margin-top: 18px; }
th, td { border-bottom: 1px solid #ccc; padding: 7px 6px; text-align: left; }
td.n, th.n { text-align: right; } .head { display: flex; justify-content: space-between; }
</style></head><body>
<div class="head"><div><h1>Vine &amp; Dine Bistro</h1>
<div class="muted">12 Marine Drive, Mumbai 400002 · orders@vineanddine.example</div></div>
<div><b>PURCHASE ORDER</b><br>PO No: VD-2026-1043<br>Date: 2026-10-16</div></div>
<p><b>To:</b> TEnsorSTudio Wine Cellar<br><b>Deliver by:</b> 2026-10-24 · <b>Payment terms:</b> 30 days</p>
<table><thead><tr><th>#</th><th>Item</th><th>Our code</th><th class="n">Qty</th><th class="n">Unit price (INR)</th></tr></thead><tbody>
<tr><td>1</td><td>Nashik Shiraz 2022 (750ml)</td><td>SHZ-NSK-22</td><td class="n">24</td><td class="n">1,450.00</td></tr>
<tr><td>2</td><td>Argentinian Malbec, 2021 vintage</td><td></td><td class="n">12</td><td class="n">2,900.00</td></tr>
<tr><td>3</td><td>NZ Sauvignon Blanc (Marlborough)</td><td></td><td class="n">12</td><td class="n">3,400.00</td></tr>
<tr><td>4</td><td>Prosecco extra dry</td><td>PROSECCO ED</td><td class="n">18</td><td class="n">2,700.00</td></tr>
<tr><td>5</td><td>Champagne brut, non-vintage</td><td></td><td class="n">6</td><td class="n">8,900.00</td></tr>
<tr><td>6</td><td>Single malt Scotch whisky 12yr</td><td></td><td class="n">2</td><td class="n">6,500.00</td></tr>
</tbody></table>
<p style="margin-top:18px">Please deliver between 10am and 4pm. Contact: Priya (purchasing).</p>
</body></html>"""


async def main() -> None:
    out = Path(__file__).with_name("wine-po-vine-and-dine.pdf")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.set_content(HTML)
        await page.pdf(path=str(out), format="A4", print_background=True)
        await browser.close()
    data = out.read_bytes()
    # The orchestrator rejects PDFs with active content; make sure ours has none.
    assert not any(m in data for m in (b"/JavaScript", b"/Launch", b"/EmbeddedFiles", b"/XFA"))
    print(f"wrote {out} ({len(data)} bytes)")


asyncio.run(main())
