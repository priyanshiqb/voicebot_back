#!/bin/bash

# ==============================================================================
# Docker System Maintenance Script
# Run this script periodically to reclaim SSD space on your server.
# ==============================================================================

echo "=========================================="
echo "🧹 Starting Docker Cleanup..."
echo "=========================================="

echo -e "\n📊 Docker Disk Usage (Before):"
docker system df

echo -e "\n🗑️  Removing stopped containers, unused networks, and dangling images..."
docker system prune -f

echo -e "\n📦 Pruning old build cache (keeping up to 2GB to maintain fast build speeds)..."
# We keep 2GB of cache so your next deployment doesn't take 5 minutes!
docker builder prune --keep-storage 2000000000 -f

echo -e "\n📊 Docker Disk Usage (After):"
docker system df

echo -e "\n✅ Docker cleanup completed successfully!"
