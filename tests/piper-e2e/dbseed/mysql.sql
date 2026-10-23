SET SESSION cte_max_recursion_depth=10000;
CREATE DATABASE IF NOT EXISTS shop CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS shop_prod CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS e2e CHARACTER SET utf8mb4;   -- the live unit tests' own (PIPER_DB_E2E_MYSQL=...:e2e)
USE shop;
CREATE TABLE customer (id INT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(200) NOT NULL, email VARCHAR(200), phone VARCHAR(50), note TEXT, created DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE orders (id INT PRIMARY KEY AUTO_INCREMENT, customer_id INT NOT NULL, total DECIMAL(10,2), status VARCHAR(20), big LONGTEXT, CONSTRAINT fk_cust FOREIGN KEY (customer_id) REFERENCES customer(id));
INSERT INTO customer (name,email,phone,note) VALUES ('Jürgen Müller','kun@example.org','+44 20 7946 0958','schöne Grüße'),('NULL guy',NULL,NULL,NULL),('<script>alert(1)</script>','x@example.org','1','$(id) `ls`'),('Emoji 🚀 עברית','e@example.org','2',REPEAT('long ',2000));
INSERT INTO customer (name,email,phone) WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n<3000) SELECT CONCAT('cust ',n), CONCAT('c',n,'@example.org'), CONCAT('+420',n) FROM s;
INSERT INTO orders (customer_id,total,status,big) WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n<3000) SELECT 1+(n%3000), n*1.5, IF(n%3=0,'paid','new'), IF(n=1,REPEAT('X',300000),NULL) FROM s;
CREATE VIEW v_paid AS SELECT o.id, c.name, o.total FROM orders o JOIN customer c ON c.id=o.customer_id WHERE o.status='paid';
CREATE DATABASE IF NOT EXISTS shop_prod;
CREATE TABLE shop_prod.customer LIKE shop.customer; INSERT INTO shop_prod.customer SELECT * FROM shop.customer WHERE id<100;
