include ../../Makefile.mk

build-rhel9:
	$(COMPOSE) up primary1-rhel9 -d --build
	$(COMPOSE) up standby1-rhel9 -d --build
	$(COMPOSE) up standby2-rhel9 -d --build
	$(COMPOSE) up standby3-rhel9 -d --build
	$(COMPOSE) up witness1-rhel9 -d --build
	$(COMPOSE) up pgbackrest1-rhel9 -d --build
	$(COMPOSE) up haproxy1-rhel9 -d --build
