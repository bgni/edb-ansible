include ../../Makefile.mk

build-rhel9:
	$(COMPOSE) up primary1-rhel9 -d
	$(COMPOSE) up standby1-rhel9 -d
	$(COMPOSE) up standby2-rhel9 -d
	$(COMPOSE) up standby3-rhel9 -d
	$(COMPOSE) up witness1-rhel9 -d
	$(COMPOSE) up pgbackrest1-rhel9 -d
	$(COMPOSE) up haproxy1-rhel9 -d
