# OCR fixture

`policy-scan-ko-en.pdf` is a synthetic, image-only PDF created for this repository. It contains no customer data or text layer.

Expected text:

- 환불 정책: 구매 후 7일 이내 환불 가능합니다.
- Refund within 7 days of purchase.

The bitmap uses Apple SD Gothic Neo rendered locally; no font program is embedded. Integration tests check key phrases and page locations rather than exact whitespace or OCR punctuation. Tests require Poppler and Tesseract with `kor` and `eng` data. CI installs both language packs and fails if unavailable; local runs skip the real OCR test when they are missing.
