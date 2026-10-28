t="temporal-sql-tool --plugin postgres12 --ep 127.0.0.1 -p 5432 -u temporal"
for db in temporal temporal_visibility; do
  $t --db $db create
  $t --db $db setup-schema -v 0.0
  $t --db $db update-schema -d /etc/temporal/schema/postgresql/v12/${db#temporal_}/versioned
done
