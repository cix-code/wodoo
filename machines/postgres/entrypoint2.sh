#!/bin/bash
set -e
cd /

echo "Executing postgres entrypoint2.sh"
echo "DBNAME: $DBNAME"


if [[ -z "$PGDATA" ]]; then
    echo "Please define pgdata"
    exit -1
fi
if [[ -n "$DO_INIT" ]]; then
    echo 'Initialising postgres'
    rm $PGDATA/* -Rf
    /docker-entrypoint.sh postgres &
    echo "Shutting down in 10 seconds..."
    sleep 10
    psql template1 -c "SELECT datname FROM pg_database WHERE datistemplate = false;"
    pkill postgres
    sleep 2
    exit 0
else
    if [[ -n "$DO_BACKUP" ]]; then
        /docker-entrypoint.sh postgres &
        sleep 10
        pg_dump $DBNAME|gzip > /opt/dumps/$DBNAME_$(date).gz
        pkill postgres
    else
        if [[ -n "$RESTORE_DUMP" ]]; then 
            echo "Restoring database $DBNAME"
            /docker-entrypoint.sh postgres &
            sleep 5
            dropdb $DBNAME || echo 'database did not exist'
            createdb $DBNAME
            gunzip -c /opt/dumps/$DBNAME.gz |psql $DBNAME
            psql template1 -c "alter database $DBNAME owner to odoo;"
            echo "Restoring snapshot done!"
            pkill postgres
        else
            echo 'Normal postgres start...'
            /docker-entrypoint.sh postgres
        fi
    fi
fi

