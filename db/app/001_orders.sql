-- app-db: the synthetic business database behind orders-api. All data is fictional.
CREATE TABLE orders (
    order_number TEXT PRIMARY KEY,
    customer     TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('open', 'shipped', 'invoiced', 'cancelled')),
    total_usd    NUMERIC(12, 2) NOT NULL CHECK (total_usd >= 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO orders (order_number, customer, status, total_usd) VALUES
    ('SO-100001', 'Northwind Traders (fictional)', 'open', 1250.00),
    ('SO-100002', 'Contoso Supply (fictional)', 'shipped', 980.50),
    ('SO-100003', 'Fabrikam Industrial (fictional)', 'invoiced', 15400.00),
    ('SO-100004', 'Tailspin Parts (fictional)', 'cancelled', 0.00);
