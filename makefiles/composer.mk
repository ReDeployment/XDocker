# makefiles/composer.mk

# 定义文件路径
BASE_COMPOSE_FILE = docker-compose.base.yaml

# 默认目标是启动所有服务
.PHONY: docker.all
docker.all:
	@echo "Starting all services..."
	@docker compose -f $(BASE_COMPOSE_FILE) up -d

docker.base.up:
	@echo "Starting base services..."
	@docker compose -f $(BASE_COMPOSE_FILE) up -d

# 停止所有服务
.PHONY: down
docker.down:
	@echo "Stopping all services..."
	@docker compose -f $(BASE_COMPOSE_FILE) down

docker.base.down:
	@echo "Starting base services..."
	@docker compose -f $(BASE_COMPOSE_FILE) down

docker.base.config:
	@docker compose -f $(BASE_COMPOSE_FILE) config