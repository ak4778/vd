PROG ?= ./vvvv       # Program we are building
OUT ?= -o $(PROG)       # Compiler argument for output file
SOURCES = main.c mongoose.c net.c data_source.c   # Source code files
# Enable mongoose's built-in TLS backend (EC P-256 + AES/ChaCha20).
# mongoose.h only defaults to MG_TLS_BUILTIN under MG_ARCH_CUBE; on Win32/Linux
# the default is MG_TLS_NONE, so HTTPS listen sockets silently fail handshake
# with "TLS is not enabled". Define it explicitly here.
CFLAGS = -W -Wall -Wextra -g3 -ggdb -O0 -fno-omit-frame-pointer -I. -DMG_TLS=MG_TLS_BUILTIN    # Build options

# RSA CRT optimisation in mongoose's built-in TLS has a bug that causes
# "CRT signing failed" during the TLS 1.2 handshake with self-signed RSA
# certs on Windows. Disable it to fall back to standard modular exponentiation.
# (only affects RSA certs; EC certs bypass this code path entirely)
CFLAGS += -DMG_TLS_RSA_USE_CRT=0

# Database mode: SQLite (default) or CSV
# To use SQLite (default): make
# To use CSV: make CSV_MODE=1
ifeq ($(CSV_MODE),1)
  CFLAGS += -DCSV_MODE
  SOURCES += csv_driver.c
  MODE = csv
else
  SOURCES += sqlite_driver.c
  # sqlite3.c is a third-party amalgamation; compile separately with relaxed warnings
  SQLITE_OBJ = sqlite3.o
  MODE = sqlite
endif

ifeq ($(OS),Windows_NT)         # Windows settings. Assume MinGW compiler.
  PROG = vvvv.exe
  CC = gcc
  CFLAGS += -lws2_32
  MODE_STAMP = .mode_$(MODE).win
  RM_MODE = -del /Q /F .mode_csv.win .mode_sqlite.win 2>nul
  TOUCH_MODE = type nul >
  DEL_CMD = cmd /C del /Q /F /S
else
  CFLAGS += -lpthread -ldl      # Link against pthread and dl for SQLite on Linux
  MODE_STAMP = .mode_$(MODE)
  RM_MODE = @rm -f .mode_csv .mode_sqlite
  TOUCH_MODE = @touch
  DEL_CMD = rm -rf
endif

all: $(PROG)
	$(RUN) $(PROG) $(ARGS)

# Compile sqlite3.c separately: suppress unused-parameter warnings in third-party code
sqlite3.o: sqlite3.c
	$(CC) -c sqlite3.c $(filter-out -lws2_32 -lpthread -ldl,$(CFLAGS)) -Wno-unused-parameter -o sqlite3.o

$(PROG): $(SOURCES) $(SQLITE_OBJ) $(MODE_STAMP)
	$(CC) $(SOURCES) $(SQLITE_OBJ) $(CFLAGS) $(CFLAGS_MONGOOSE) $(CFLAGS_EXTRA) $(OUT)

$(MODE_STAMP):
	$(RM_MODE)
	$(TOUCH_MODE) $@
	@echo === Building in $(MODE) mode ===

web_root/bundle.js:
	curl -s https://npm.reversehttp.com/preact,preact/hooks,htm/preact,preact-router -o $@

clean:
	$(DEL_CMD) $(PROG) $(PACK) *.o *.obj *.exe *.dSYM .mode_*
