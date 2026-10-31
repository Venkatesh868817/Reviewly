# Reviewly project map

## Request flow

1. `static/index.html` presents the product URL form and results area.
2. `static/script.js` sends the URL to Flask at `/api/analyze`, renders the response, and prepares the reviews CSV download in the browser.
3. `app.py` fetches/parses product reviews, performs VADER sentiment labeling, runs review checks, and returns JSON to the page.
4. `data/product-reviews.csv` is a local sample/export used as input by the transformer notebook.
5. `notebooks/Reviewly_Transformer_Comparison.ipynb` demonstrates DistilBERT inference and optional BERT/ELECTRA/DistilBERT training/evaluation.
6. `outputs/` is reserved for generated notebook results, metrics, and model checkpoints.
7. `tests/test_app.py` contains offline checks for scraper and API behavior.

The live web app currently uses VADER. Transformer experiments in the notebook are independent and do not affect the app's predictions. Rating-derived labels are weak labels; use human-reviewed labels and a representative dataset when comparing model quality.
