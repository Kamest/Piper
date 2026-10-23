-- sqlcmd -C -U sa: databases shop + shop_prod (SQL Server's "schema" in Piper = the database, like MySQL)
SET NOCOUNT ON;
CREATE DATABASE shop COLLATE Latin1_General_100_CI_AS_SC_UTF8;
CREATE DATABASE shop_prod COLLATE Latin1_General_100_CI_AS_SC_UTF8;
GO
USE shop;
CREATE TABLE customer (id INT IDENTITY(1,1) PRIMARY KEY, name NVARCHAR(200) NOT NULL, email NVARCHAR(200), phone NVARCHAR(50), note NVARCHAR(MAX), created DATETIME2 DEFAULT SYSUTCDATETIME());
CREATE TABLE orders (id INT IDENTITY(1,1) PRIMARY KEY, customer_id INT NOT NULL, total DECIMAL(10,2), status NVARCHAR(20), big NVARCHAR(MAX), CONSTRAINT fk_cust FOREIGN KEY (customer_id) REFERENCES customer(id));
INSERT INTO customer (name,email,phone,note) VALUES (N'Jürgen Müller',N'kun@example.org',N'+44 20 7946 0958',N'schöne Grüße'),(N'NULL guy',NULL,NULL,NULL),(N'<script>alert(1)</script>',N'x@example.org',N'1',N'$(id) `ls`'),(N'Emoji 🚀 עברית',N'e@example.org',N'2',REPLICATE(CAST(N'long ' AS NVARCHAR(MAX)),2000));
WITH s(n) AS (SELECT TOP 3000 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) FROM sys.all_objects a CROSS JOIN sys.all_objects b)
INSERT INTO customer (name,email,phone) SELECT CONCAT(N'cust ',n), CONCAT(N'c',n,N'@example.org'), CONCAT(N'+420',n) FROM s;
WITH s(n) AS (SELECT TOP 3000 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) FROM sys.all_objects a CROSS JOIN sys.all_objects b)
INSERT INTO orders (customer_id,total,status,big) SELECT 1+(n%3000), n*1.5, CASE WHEN n%3=0 THEN N'paid' ELSE N'new' END, CASE WHEN n=1 THEN REPLICATE(CAST(N'X' AS NVARCHAR(MAX)),300000) END FROM s;
GO
CREATE VIEW v_paid AS SELECT o.id, c.name, o.total FROM orders o JOIN customer c ON c.id=o.customer_id WHERE o.status='paid';
GO
USE shop_prod;
SELECT * INTO customer FROM shop.dbo.customer WHERE id < 100;
ALTER TABLE customer ADD CONSTRAINT pk_customer PRIMARY KEY (id);
GO
