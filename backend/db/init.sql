PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- SQLite schema for the shopping-guide MVP.
-- SQLite stores deterministic product facts, structured detail content, SKU prices, and carts.
-- Chroma handles long-text semantic retrieval; product_id and source_index align records across both stores.

-- products: primary table for stable product-level facts.
-- base_price preserves the source JSON value for display and coarse sorting. Hard budget filters
-- should prefer product_skus.price or product_price_ranges.min_price.
CREATE TABLE IF NOT EXISTS products (
    product_id   TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    brand        TEXT NOT NULL,
    category     TEXT NOT NULL,
    sub_category TEXT,
    base_price   REAL NOT NULL CHECK (base_price >= 0),
    image_path   TEXT,
    image_url    TEXT,
    source_path  TEXT,
    status       TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive')),
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (image_path IS NOT NULL OR image_url IS NOT NULL)
);

-- product_skus: source of truth for variants, SKU prices, and cart settlement.
-- Budget filters should check whether at least one SKU satisfies the price constraint.
CREATE TABLE IF NOT EXISTS product_skus (
    sku_id          TEXT PRIMARY KEY,
    product_id      TEXT NOT NULL,
    properties_json TEXT NOT NULL,
    price           REAL NOT NULL CHECK (price >= 0),
    stock_qty       INTEGER NOT NULL DEFAULT 999 CHECK (stock_qty >= 0),
    status          TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive')),
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE,
    CHECK (json_valid(properties_json))
);

-- product_descriptions: marketing descriptions, selling points, and usage guidance.
-- Product detail pages read this table directly; Chroma marketing chunks use the same text.
CREATE TABLE IF NOT EXISTS product_descriptions (
    product_id            TEXT PRIMARY KEY,
    marketing_description TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

-- product_faqs: official FAQ entries.
-- source_index aligns with the FAQ index in Chroma chunk IDs for relational lookup after retrieval.
CREATE TABLE IF NOT EXISTS product_faqs (
    faq_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   TEXT NOT NULL,
    source_index INTEGER NOT NULL,
    question     TEXT NOT NULL DEFAULT '',
    answer       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE,
    UNIQUE (product_id, source_index)
);

-- product_reviews: individual customer reviews.
-- Used by product detail views, review statistics, risk notices, and lookups for Chroma review chunks.
CREATE TABLE IF NOT EXISTS product_reviews (
    review_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   TEXT NOT NULL,
    source_index INTEGER NOT NULL,
    nickname     TEXT NOT NULL DEFAULT '',
    rating       INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    content      TEXT NOT NULL DEFAULT '',
    polarity     TEXT NOT NULL DEFAULT 'neutral'
        CHECK (polarity IN ('positive', 'neutral', 'negative')),
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE,
    UNIQUE (product_id, source_index)
);

-- users: demo user table that provides ownership for carts and future preference or session features.
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    nickname   TEXT NOT NULL DEFAULT 'demo_user',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- cart_items: demo cart table storing the selected SKU, quantity, and purchase-price snapshot.
CREATE TABLE IF NOT EXISTS cart_items (
    cart_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    product_id   TEXT NOT NULL,
    sku_id       TEXT NOT NULL,
    selected_options_json TEXT NOT NULL DEFAULT '{}',
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    unit_price   REAL NOT NULL CHECK (unit_price >= 0),
    selected     INTEGER NOT NULL DEFAULT 1 CHECK (selected IN (0, 1)),
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(product_id),
    FOREIGN KEY (sku_id) REFERENCES product_skus(sku_id),
    CHECK (json_valid(selected_options_json)),
    UNIQUE (user_id, sku_id)
);

-- product_price_ranges: product price-range view.
-- Product-card starting prices and budget-filtered recommendations should prefer this view's min_price.
CREATE VIEW IF NOT EXISTS product_price_ranges AS
SELECT
    product_id,
    MIN(price) AS min_price,
    MAX(price) AS max_price,
    COUNT(*) AS sku_count
FROM product_skus
WHERE status = 'active'
GROUP BY product_id;

CREATE INDEX IF NOT EXISTS idx_products_category
    ON products(category, sub_category);
CREATE INDEX IF NOT EXISTS idx_products_brand
    ON products(brand);
CREATE INDEX IF NOT EXISTS idx_products_base_price
    ON products(base_price);
CREATE INDEX IF NOT EXISTS idx_product_skus_product
    ON product_skus(product_id);
CREATE INDEX IF NOT EXISTS idx_product_skus_price
    ON product_skus(price);
CREATE INDEX IF NOT EXISTS idx_product_descriptions_product
    ON product_descriptions(product_id);
CREATE INDEX IF NOT EXISTS idx_product_faqs_product
    ON product_faqs(product_id);
CREATE INDEX IF NOT EXISTS idx_product_reviews_product
    ON product_reviews(product_id);
CREATE INDEX IF NOT EXISTS idx_product_reviews_polarity
    ON product_reviews(product_id, polarity);
CREATE INDEX IF NOT EXISTS idx_cart_items_user
    ON cart_items(user_id);

CREATE TRIGGER IF NOT EXISTS trg_products_updated_at
AFTER UPDATE ON products
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE products SET updated_at = CURRENT_TIMESTAMP WHERE product_id = NEW.product_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_product_skus_updated_at
AFTER UPDATE ON product_skus
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE product_skus SET updated_at = CURRENT_TIMESTAMP WHERE sku_id = NEW.sku_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_product_descriptions_updated_at
AFTER UPDATE ON product_descriptions
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE product_descriptions SET updated_at = CURRENT_TIMESTAMP WHERE product_id = NEW.product_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_product_faqs_updated_at
AFTER UPDATE ON product_faqs
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE product_faqs SET updated_at = CURRENT_TIMESTAMP WHERE faq_id = NEW.faq_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_product_reviews_updated_at
AFTER UPDATE ON product_reviews
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE product_reviews SET updated_at = CURRENT_TIMESTAMP WHERE review_id = NEW.review_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_cart_items_updated_at
AFTER UPDATE ON cart_items
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE cart_items SET updated_at = CURRENT_TIMESTAMP WHERE cart_item_id = NEW.cart_item_id;
END;

INSERT OR IGNORE INTO users(user_id, nickname)
VALUES ('demo_user', 'Demo User');
