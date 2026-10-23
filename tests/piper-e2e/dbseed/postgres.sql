CREATE SCHEMA shop;
SET search_path=shop;
CREATE TABLE customer (id SERIAL PRIMARY KEY, name VARCHAR(200) NOT NULL, email VARCHAR(200), phone VARCHAR(50), note TEXT, created TIMESTAMP DEFAULT now());
CREATE TABLE orders (id SERIAL PRIMARY KEY, customer_id INT NOT NULL REFERENCES customer(id), total NUMERIC(10,2), status VARCHAR(20), big TEXT, data JSONB);
INSERT INTO customer (name,email,phone,note) VALUES ('Jürgen Müller','kun@example.org','+44 20 7946 0958','schöne Grüße'),('NULL guy',NULL,NULL,NULL),('<script>alert(1)</script>','x@example.org','1','$(id) `ls`'),('Emoji 🚀 עברית','e@example.org','2',repeat('long ',2000));
INSERT INTO customer (name,email,phone) SELECT 'cust '||n, 'c'||n||'@example.org', '+420'||n FROM generate_series(1,3000) n;
INSERT INTO orders (customer_id,total,status,big,data) SELECT 1+(n%3000), n*1.5, CASE WHEN n%3=0 THEN 'paid' ELSE 'new' END, CASE WHEN n=1 THEN repeat('X',300000) END, jsonb_build_object('n',n) FROM generate_series(1,3000) n;
CREATE VIEW v_paid AS SELECT o.id, c.name, o.total FROM orders o JOIN customer c ON c.id=o.customer_id WHERE o.status='paid';
