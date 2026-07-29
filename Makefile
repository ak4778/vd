PROG ?= ./vvvv       # Program we are building
OUT ?= -o $(PROG)       # Compiler argument for output file
SOURCES = main.c mongoose.c net.c data_source.c   # Source code files
CFLAGS = -W -Wall -Wextra -g3 -ggdb -O0 -fno-omit-frame-pointer -I.                # Build options

# Database mode: SQLite (default) or CSV
# To use SQLite (default): make
# To use CSV: make CSV_MODE=1
ifeq ($(CSV_MODE),1)
  CFLAGS += -DCSV_MODE
  MODE = csv
else
  SOURCES += sqlite3.c
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

$(PROG): $(SOURCES) $(MODE_STAMP)
	$(CC) $(SOURCES) $(CFLAGS) $(CFLAGS_MONGOOSE) $(CFLAGS_EXTRA) $(OUT)

$(MODE_STAMP):
	$(RM_MODE)
	$(TOUCH_MODE) $@
	@echo === Building in $(MODE) mode ===

web_root/bundle.js:
	curl -s https://npm.reversehttp.com/preact,preact/hooks,htm/preact,preact-router -o $@

clean:
	$(DEL_CMD) $(PROG) $(PACK) *.o *.obj *.exe *.dSYM .mode_*