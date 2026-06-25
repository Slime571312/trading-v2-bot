#!/bin/bash
# Start Prometheus + Grafana für Trading-Bot v2

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "🚀 Starting Trading-Bot Monitoring Stack..."
echo ""

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker is not running. Please start Docker first."
    exit 1
fi

# Start services
cd "$SCRIPT_DIR"
echo "📦 Starting Prometheus + Grafana..."
docker-compose -f docker-compose-monitoring.yml up -d

# Wait for services to be healthy
echo ""
echo "⏳ Waiting for services to be ready..."

MAX_RETRIES=30
RETRY=0

# Wait for Prometheus
echo -n "  ⏳ Prometheus"
until curl -s http://localhost:9090/-/healthy > /dev/null 2>&1; do
    RETRY=$((RETRY + 1))
    if [ $RETRY -gt $MAX_RETRIES ]; then
        echo " ❌"
        echo "❌ Prometheus failed to start"
        exit 1
    fi
    echo -n "."
    sleep 1
done
echo " ✓"
RETRY=0

# Wait for Grafana
echo -n "  ⏳ Grafana"
until curl -s http://localhost:3001/api/health > /dev/null 2>&1; do
    RETRY=$((RETRY + 1))
    if [ $RETRY -gt $MAX_RETRIES ]; then
        echo " ❌"
        echo "❌ Grafana failed to start"
        exit 1
    fi
    echo -n "."
    sleep 1
done
echo " ✓"

echo ""
echo "✅ Monitoring Stack is ready!"
echo ""
echo "📊 Dashboards:"
echo "  • Prometheus:  http://localhost:9090"
echo "  • Grafana:     http://localhost:3001 (admin / simontrading123)"
echo ""
echo "📝 Next steps:"
echo "  1. Open http://localhost:3001 in your browser"
echo "  2. Go to Configuration > Data Sources"
echo "  3. Verify Prometheus datasource is configured"
echo "  4. Create 3 dashboards:"
echo "     - Trading-Bot Status (real-time)"
echo "     - Performance-Analytics (weekly review)"
echo "     - System-Health (ops)"
echo ""
echo "💡 Tips:"
echo "  • Prometheus data is stored in: prometheus_data/"
echo "  • Stop with: docker-compose -f docker-compose-monitoring.yml down"
echo "  • View logs: docker-compose -f docker-compose-monitoring.yml logs -f"
echo ""
