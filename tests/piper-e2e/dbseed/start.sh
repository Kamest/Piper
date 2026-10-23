#!/bin/sh
# Throwaway MySQL 8 + PostgreSQL 17 for tests/piper-e2e/dbprod.py (data on tmpfs, gone with the container).
#   sh tests/piper-e2e/dbseed/start.sh [suffix]    ->  piper-test-mysql<suffix> :13306, piper-test-pg<suffix> :15432
#   WITH="oracle mssql" sh ...start.sh             ->  + piper-test-oracle<suffix> :11521 (gvenzl/oracle-free:slim, ~1.5 GB,
#                                                      user SHOP in FREEPDB1), piper-test-mssql<suffix> :11433 (SQL Server 2022,
#                                                      linux/amd64 under Rosetta, ~1.6 GB; databases shop + shop_prod)
#   docker rm -f piper-test-mysql<suffix> piper-test-pg<suffix> [piper-test-oracle<suffix> piper-test-mssql<suffix>]   when done
# Ports: MYSQL_PORT / PG_PORT / ORA_PORT / MSSQL_PORT (then DBPROD_MYSQL / DBPROD_PG / DBPROD_ORACLE / DBPROD_MSSQL for
# dbprod.py and PIPER_DB_E2E_* for the live unit tests - the line to export is printed at the end).
set -e
D=$(cd "$(dirname "$0")" && pwd); S=${1:-}
MP=${MYSQL_PORT:-13306}; PP=${PG_PORT:-15432}
docker run -d --name piper-test-mysql$S -p 127.0.0.1:$MP:3306 --tmpfs /var/lib/mysql \
  -e MYSQL_ROOT_PASSWORD=e2e-root-pw mysql:8.0 --character-set-server=utf8mb4 >/dev/null
docker run -d --name piper-test-pg$S -p 127.0.0.1:$PP:5432 --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_PASSWORD=e2e-pg-pw -e POSTGRES_DB=e2e postgres:17 >/dev/null
i=0; until docker exec piper-test-mysql$S mysqladmin -h127.0.0.1 --protocol=tcp -uroot -pe2e-root-pw ping >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 90 ] && exit 1; sleep 1; done
i=0; until docker exec piper-test-pg$S pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 60 ] && exit 1; sleep 1; done
# TCP, not the socket: the images' init server answers on the socket before the real one (seed lost)
sleep 2
# utf8mb4 on the client too: without it the seed's unicode lands as mojibake
docker exec -i piper-test-mysql$S mysql -uroot -pe2e-root-pw --default-character-set=utf8mb4 < "$D/mysql.sql" 2>/dev/null
docker exec -i piper-test-pg$S psql -q -U postgres -d e2e -v ON_ERROR_STOP=1 < "$D/postgres.sql" >/dev/null
echo "up: mysql 127.0.0.1:$MP, postgres 127.0.0.1:$PP"
OP=${ORA_PORT:-11521}; QP=${MSSQL_PORT:-11433}
case " $WITH " in *" oracle "*)
  docker run -d --name piper-test-oracle$S -p 127.0.0.1:$OP:1521 -e ORACLE_PASSWORD=e2e-ora-sys -e APP_USER=shop \
    -e APP_USER_PASSWORD=e2e-ora-pw gvenzl/oracle-free:slim >/dev/null
  i=0; until docker logs piper-test-oracle$S 2>&1 | grep -q "DATABASE IS READY TO USE"; do i=$((i+1)); [ $i -gt 300 ] && exit 1; sleep 1; done
  docker exec -i -e NLS_LANG=AMERICAN_AMERICA.AL32UTF8 piper-test-oracle$S sqlplus -s shop/e2e-ora-pw@//localhost/FREEPDB1 \
    < "$D/oracle.sql" >/dev/null
  echo "up: oracle 127.0.0.1:$OP  DBPROD_ORACLE=127.0.0.1:$OP:shop:e2e-ora-pw:FREEPDB1";;
esac
case " $WITH " in *" mssql "*)
  docker run -d --name piper-test-mssql$S --platform linux/amd64 -p 127.0.0.1:$QP:1433 -e ACCEPT_EULA=Y \
    -e 'MSSQL_SA_PASSWORD=E2e-mssql-pw!' mcr.microsoft.com/mssql/server:2022-latest >/dev/null
  i=0; until docker exec piper-test-mssql$S /opt/mssql-tools18/bin/sqlcmd -C -U sa -P 'E2e-mssql-pw!' -Q "SELECT 1" >/dev/null 2>&1
  do i=$((i+1)); [ $i -gt 180 ] && exit 1; sleep 1; done
  # -x: no sqlcmd variables - the seed holds a literal $(id)
  docker exec -i piper-test-mssql$S /opt/mssql-tools18/bin/sqlcmd -C -U sa -P 'E2e-mssql-pw!' -b -x < "$D/mssql.sql" >/dev/null
  echo "up: mssql 127.0.0.1:$QP  DBPROD_MSSQL='127.0.0.1:$QP:sa:E2e-mssql-pw!:shop'";;
esac
