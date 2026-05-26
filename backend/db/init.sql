PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 电商导购 MVP SQLite schema。
-- SQLite 负责确定性商品事实、详情页结构化内容、SKU 价格和购物车。
-- Chroma 负责长文本语义召回；两者通过 product_id + source_index 对齐。

-- products：商品主表，保存商品级稳定事实。
-- base_price 保留原始 JSON 中的基础价，适合展示和粗排序；预算 hard filter
-- 应优先使用 product_skus.price 或 product_price_ranges.min_price。
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

-- product_skus：SKU 表，是规格、SKU 价格和购物车结算的真实来源。
-- “500 元以内”这类预算过滤应判断是否存在满足价格条件的 SKU。
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

-- product_descriptions：商品营销描述/卖点/使用建议。
-- 详情页直接读取本表；Chroma 中的 marketing chunk 也来自同一份文本。
CREATE TABLE IF NOT EXISTS product_descriptions (
    product_id            TEXT PRIMARY KEY,
    marketing_description TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

-- product_faqs：官方 FAQ 明细表。
-- source_index 与 Chroma chunk_id 中的 faq index 对齐，用于召回后回表。
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

-- product_reviews：用户评价明细表。
-- 供商品详情页展示、评价统计、风险提示和 Chroma review chunk 回表使用。
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

-- users：演示用户表，为购物车和后续偏好/会话扩展预留用户归属。
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    nickname   TEXT NOT NULL DEFAULT 'demo_user',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- cart_items：演示购物车表，保存用户选择的具体 SKU、数量和下单价格快照。
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

-- product_price_ranges：商品价格区间视图。
-- 商品卡片展示“xx 元起”和预算过滤推荐优先使用本视图的 min_price。
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
