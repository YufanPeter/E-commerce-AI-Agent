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
    rating_number INTEGER NOT NULL CHECK (rating_number >= 0),
    source_brand TEXT NOT NULL,
    is_generic_brand INTEGER NOT NULL DEFAULT 0 CHECK (is_generic_brand IN (0, 1)),
    catalog_group TEXT,
    product_role TEXT NOT NULL CHECK (product_role IN ('primary', 'accessory')),
    audience TEXT,
    dup_key TEXT,
    short_description INTEGER NOT NULL DEFAULT 0 CHECK (short_description IN (0, 1))
);

CREATE INDEX idx_amazon_product_category_path ON amazon_product_metadata(category_path);
CREATE INDEX idx_amazon_product_group ON amazon_product_metadata(catalog_group);

-- Controlled category vocabulary; sub_category values on products reference label.
CREATE TABLE catalog_taxonomy (
    label TEXT PRIMARY KEY,
    category_path TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    catalog_group TEXT,
    product_role TEXT NOT NULL CHECK (product_role IN ('primary', 'accessory')),
    audience TEXT,
    terms_json TEXT NOT NULL CHECK (json_valid(terms_json)),
    accessory_for_json TEXT NOT NULL CHECK (json_valid(accessory_for_json))
);

-- Listing facts with their source span. A missing row means the listing did not say.
CREATE TABLE product_attributes (
    product_id TEXT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('observed', 'ambiguous')),
    evidence_field TEXT NOT NULL,
    evidence_start INTEGER NOT NULL,
    evidence_end INTEGER NOT NULL,
    provenance TEXT NOT NULL,
    PRIMARY KEY (product_id, attribute, value)
);

CREATE INDEX idx_product_attributes_lookup ON product_attributes(attribute, value);

CREATE TABLE catalog_build_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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
