#!/bin/bash
set -x

cd /opt/src

pkill -9 -f ariconnector

echo "Starting ARI Connector...."
cd /opt/src/asterisk_ari/connector
python ariconnector.py \
    --odoo-host $ODOO_HOST \
    --odoo-port $ODOO_PORT \
    --odoo-user $ODOO_USER \
    --odoo-password $ODOO_PASSWORD \
    --odoo-db $ODOO_DB
