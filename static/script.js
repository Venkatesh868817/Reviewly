let currentReviews = [];
let currentProductUrl = '';

const reviewRows = document.getElementById('rows');
const urlInput = document.getElementById('url');
const errorMessage = document.getElementById('error');
const statusMessage = document.getElementById('status');
const analyzeButton = document.getElementById('analyze');
const resultsSection = document.getElementById('results');
const exportButton = document.getElementById('export');

function showError(message) {
  errorMessage.textContent = message;
  errorMessage.style.display = 'block';
  statusMessage.style.display = 'none';
}

function renderReviews(reviews) {
  reviewRows.replaceChildren();

  reviews.forEach((review) => {
    const row = document.createElement('tr');
    const textCell = document.createElement('td');
    const ratingCell = document.createElement('td');
    const sentimentCell = document.createElement('td');
    const checksCell = document.createElement('td');
    const dateCell = document.createElement('td');
    const stars = document.createElement('span');
    const sentiment = document.createElement('span');

    textCell.className = 'review';
    textCell.textContent = review.review;
    stars.className = 'stars';
    stars.textContent = review.rating
      ? '★'.repeat(Math.round(review.rating)) + '☆'.repeat(5 - Math.round(review.rating))
      : 'Not rated';
    ratingCell.append(stars);

    sentiment.className = `sentiment ${review.sentiment.toLowerCase()}`;
    sentiment.textContent = review.sentiment;
    sentimentCell.append(sentiment);
    checksCell.className = 'review-checks';
    checksCell.textContent = review.checks?.length ? review.checks.join(', ') : 'None';
    dateCell.textContent = review.date || '—';
    row.append(textCell, ratingCell, sentimentCell, checksCell, dateCell);
    reviewRows.append(row);
  });
}

async function analyzeProduct() {
  const productUrl = urlInput.value.trim();

  let parsedUrl;

  try {
    parsedUrl = new URL(productUrl);
    if (!['http:', 'https:'].includes(parsedUrl.protocol)) {
      throw new Error('Enter a valid HTTP or HTTPS product URL.');
    }
  } catch {
    showError('Enter a valid product URL, such as https://store.com/product.');
    urlInput.focus();
    return;
  }

  errorMessage.style.display = 'none';
  statusMessage.textContent = 'Fetching and analyzing product reviews…';
  statusMessage.style.display = 'block';
  currentReviews = [];
  currentProductUrl = '';
  exportButton.disabled = true;
  resultsSection.classList.add('hidden');
  analyzeButton.disabled = true;

  try {
    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: productUrl }),
    });
    const contentType = response.headers.get('content-type') || '';
    let result;

    if (contentType.includes('application/json')) {
      try {
        result = await response.json();
      } catch {
        throw new Error('The backend returned an incomplete JSON response. Please restart it and try again.');
      }
    } else {
      const responseText = await response.text();

      if (response.status === 404) {
        throw new Error('The backend API was not found. Start app.py and open the site at http://127.0.0.1:5000.');
      }

      throw new Error(
        responseText.trim()
          ? 'The backend returned a non-JSON response. Make sure you opened the site through Flask at http://127.0.0.1:5000.'
          : `Received an empty HTTP ${response.status} response from ${response.url}. Reviewly should return JSON here. Hard-refresh the page (Ctrl+F5) and confirm the Flask window logs a POST to /api/analyze when you click Analyze.`
      );
    }

    if (!response.ok) {
      throw new Error(result.error || 'Could not analyze this product page.');
    }

    currentReviews = result.reviews;
    currentProductUrl = productUrl;
    exportButton.disabled = currentReviews.length === 0;
    renderReviews(currentReviews);
    document.getElementById('product').textContent =
      `Reviews from ${parsedUrl.hostname}`;
    document.getElementById('total-reviews').textContent = result.total_reviews;
    document.getElementById('positive-percent').textContent =
      `${result.positive_percentage}%`;
    document.getElementById('positive-count').textContent =
      `${result.positive_count} positive reviews`;
    document.getElementById('average-rating').textContent =
      result.average_rating ?? 'N/A';
    document.getElementById('rated-reviews').textContent =
      `${result.rated_reviews} rated review${result.rated_reviews === 1 ? '' : 's'} out of 5`;
    document.getElementById('common-theme').textContent = result.common_theme;
    document.getElementById('common-theme-foot').textContent =
      `${result.top_words?.[0]?.reviews ?? 0} reviews mention the top word`;
    document.getElementById('short-checks').textContent =
      result.review_checks?.short_text_count ?? 0;
    document.getElementById('mismatch-checks').textContent =
      result.review_checks?.rating_text_mismatch_count ?? 0;

    statusMessage.style.display = 'none';
    resultsSection.classList.remove('hidden');
    resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    const message = error instanceof TypeError
      ? 'Could not reach the backend. Start app.py and open the site at http://127.0.0.1:5000.'
      : error.message || 'The server could not analyze that page.';
    showError(message);
  } finally {
    analyzeButton.disabled = false;
  }
}

analyzeButton.addEventListener('click', analyzeProduct);
urlInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') analyzeProduct();
});

exportButton.addEventListener('click', () => {
  if (!currentReviews.length) return;

  const escapeCsvValue = (value) =>
    `"${String(value ?? '').replaceAll('"', '""')}"`;
  const rows = [
    ['Product URL', 'Review', 'Rating', 'Text sentiment', 'Rating sentiment', 'Review checks', 'Author', 'Date'],
    ...currentReviews.map((review) => [
      currentProductUrl,
      review.review,
      review.rating ?? '',
      review.sentiment,
      review.rating_sentiment,
      (review.checks || []).join('; '),
      review.author,
      review.date,
    ]),
  ];
  const csv = rows
    .map((row) => row.map(escapeCsvValue).join(','))
    .join('\r\n');
  const file = new Blob(['\ufeff' + csv], {
    type: 'text/csv;charset=utf-8',
  });
  const fileUrl = URL.createObjectURL(file);
  const link = document.createElement('a');
  const productHost = new URL(currentProductUrl).hostname.replace(/[^a-z0-9.-]/gi, '-');

  link.href = fileUrl;
  link.download = `reviewly-reviews-${productHost || 'product'}.csv`;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(fileUrl), 1000);
});



