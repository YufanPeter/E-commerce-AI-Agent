PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- MVP SQLite schema for the ecommerce shopping guide.
-- Keep only deterministic business data needed by the App:
-- product detail, SKU selection, product content display, and one cart per user.
-- Long product content is stored as JSON text for frontend display and can
-- also be chunked into the RAG vector store during import.

CREATE TABLE IF NOT EXISTS products (
    product_id   TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    brand        TEXT NOT NULL,
    category     TEXT NOT NULL,
    sub_category TEXT,
    summary      TEXT NOT NULL DEFAULT '',
    recommend_reason TEXT NOT NULL DEFAULT '',
    tags_json    TEXT NOT NULL DEFAULT '[]',
    base_price   REAL NOT NULL CHECK (base_price >= 0),
    min_price    REAL NOT NULL CHECK (min_price >= 0),
    max_price    REAL NOT NULL CHECK (max_price >= 0),
    image_path   TEXT,
    image_url    TEXT,
    source_path  TEXT,
    status       TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive')),
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (min_price <= max_price),
    CHECK (image_path IS NOT NULL OR image_url IS NOT NULL),
    CHECK (json_valid(tags_json))
);

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

CREATE TABLE IF NOT EXISTS product_contents (
    product_id            TEXT PRIMARY KEY,
    marketing_description TEXT NOT NULL DEFAULT '',
    official_faq_json     TEXT NOT NULL DEFAULT '[]',
    user_reviews_json     TEXT NOT NULL DEFAULT '[]',
    created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE,
    CHECK (json_valid(official_faq_json)),
    CHECK (json_valid(user_reviews_json))
);

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    nickname   TEXT NOT NULL DEFAULT 'demo_user',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

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

CREATE INDEX IF NOT EXISTS idx_products_category_price
    ON products(category, sub_category, min_price, max_price);
CREATE INDEX IF NOT EXISTS idx_products_brand
    ON products(brand);
CREATE INDEX IF NOT EXISTS idx_product_skus_product
    ON product_skus(product_id);
CREATE INDEX IF NOT EXISTS idx_product_skus_price
    ON product_skus(price);
CREATE INDEX IF NOT EXISTS idx_product_contents_product
    ON product_contents(product_id);
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

CREATE TRIGGER IF NOT EXISTS trg_product_contents_updated_at
AFTER UPDATE ON product_contents
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE product_contents SET updated_at = CURRENT_TIMESTAMP WHERE product_id = NEW.product_id;
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
