# Reviewly

Reviewly collects product reviews, labels text sentiment, flags reviews for human inspection, summarizes common themes, and exports analysis results to CSV. The Flask app includes a Flipkart reader for review data embedded in the page's initial JSON state, plus generic HTML/JSON-LD fallbacks.

## Project layout

```text
Review/
├── app.py                          # Flask routes, scraping, sentiment analysis, and API
├── requirements.txt                # Python dependencies for the web app
├── README.md                       # Setup, usage, API, and project guide
├── .gitignore                      # Local environments and generated artifacts
├── static/                         # Browser files served by Flask
│   ├── index.html                  # Reviewly page structure
│   ├── styles.css                  # Page presentation
│   └── script.js                   # Analyze flow, results, and CSV download
├── data/
│   └── product-reviews.csv         # Sample/exported review data for the notebook
├── notebooks/
│   └── Reviewly_Transformer_Comparison.ipynb # BERT, ELECTRA, and DistilBERT experiments
├── tests/
│   ├── __init__.py                 # Test package marker
│   └── test_app.py                 # Offline scraper and API tests
├── docs/                           # Additional project documentation
└── outputs/                        # Generated notebook predictions, models, and metrics
```

The app labels text with VADER. The transformer comparison is a separate notebook experiment; it does not change which model the web app uses. Review flags are prompts for human inspection and do not prove a review is fake or incorrect.

## Run on Windows

Open PowerShell in this folder and run:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:SCRAPEDO_TOKEN = Read-Host "Scrape.do token (optional; press Enter to skip)"
.\.venv\Scripts\python.exe app.py
```

Then visit <http://127.0.0.1:5000>. Keep the server running while using the page. Do not open `static/index.html` directly: the browser needs Flask for `/api/analyze`.

## API and CSV export

`POST /api/analyze` accepts JSON such as `{"url": "https://shop.example/product"}`. The response includes extracted reviews, VADER text sentiment, review checks, check counts, positive share, average available rating, and a common-word summary. Select **Download reviews CSV** after analysis to save the product URL, review text, star rating, text sentiment, rating sentiment, checks, author, and date.

## Scraper coverage and limitations

Flipkart review data is read from its embedded page state. Without `SCRAPEDO_TOKEN`, the backend tries a regular HTTP request, which may be blocked or may not include dynamically loaded reviews. With a token, it can ask Scrape.do to render JavaScript. Other sites are checked for Schema.org JSON-LD and common review markup. Sites requiring sign-in or rendering reviews in a different way may need a dedicated scraper or authorized API. Follow each site's terms and access rules.

Flipkart's embedded page structure can change and requests may be blocked. The app mixes helpful and recent review pages, removes duplicates, caches results for 15 minutes, and warns when fewer than 20 reviews are found. Scrape.do rendering can use additional credits; check its current pricing before scraping many pages.

## Transformer notebook

Open `notebooks/Reviewly_Transformer_Comparison.ipynb` in Jupyter. It reads `data/product-reviews.csv` and writes predictions, model checkpoints, and comparison metrics under `outputs/`. The quick DistilBERT cell is an inference demonstration; meaningful model comparison requires enough representative, correctly labeled examples. Star ratings used as labels are weak labels, not independently verified sentiment.

## Settings

| Variable | Purpose |
| --- | --- |
| `SCRAPEDO_TOKEN` | Optional Scrape.do token for rendered fetches. |
| `SCRAPEDO_SUPER` | Set to `1` to request Scrape.do's premium proxy pool. |
| `SCRAPE_DEBUG_DIR` | Optional directory for raw HTML when review parsing fails. |

## Offline checks

Run the existing tests from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest -v tests.test_app
```
