include ../../Makefile.mk

build-rhel9:
	$(COMPOSE) up primary1-rhel9 -d --build
	$(COMPOSE) up standby1-rhel9 -d --build
	$(COMPOSE) up standby2-rhel9 -d --build
	$(COMPOSE) up standby3-rhel9 -d --build
