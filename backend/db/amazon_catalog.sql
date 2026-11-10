-- Applied only to a new Amazon snapshot database.
CREATE TABLE amazon_product_metadata (
    product_id TEXT PRIMARY KEY REFERENCES products(product_id) ON DELETE CASCADE,
    parent_asin TEXT NOT NULL UNIQUE,
    dataset TEXT NOT NULL,
    category_path TEXT NOT NULL,
    source_category TEXT NOT NULL,
    price_usd REAL NOT NULL CHECK (price_usd > 0),
    usd_cny REAL NOT NULL CHECK (usd_cny > 0),
    average_rating REAL NOT NULL CHECK (average_rating BETWEEN 0 AND 5),
    rating_number INTEGER NOT NULL CHECK (rating_number >= 0)
);

CREATE INDEX idx_amazon_product_category_path ON amazon_product_metadata(category_path);

-- Preserve the price range contract for catalog queries without inventing a SKU.
DROP VIEW product_price_ranges;
CREATE VIEW product_price_ranges AS
SELECT product_id, MIN(price) AS min_price, MAX(price) AS max_price, COUNT(*) AS sku_count
FROM product_skus
WHERE status = 'active'
GROUP BY product_id
UNION ALL
SELECT p.product_id, p.base_price, p.base_price, 0
FROM products p JOIN amazon_product_metadata a ON p.product_id = a.product_id
WHERE NOT EXISTS (
    SELECT 1 FROM product_skus s WHERE s.product_id = p.product_id AND s.status = 'active'
);
