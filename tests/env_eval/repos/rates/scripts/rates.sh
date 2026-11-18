#!/bin/sh
# Today's euro rates, from the rates service.
set -e
node -e 'const isNumber = require("is-number"); if (!isNumber(1)) process.exit(1)'
echo "Fetching today's rates..."
curl -sS --fail-with-body https://rates.example.com/v1/eur.json
