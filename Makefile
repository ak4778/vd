PROG ?= ./vvvv       # Program we are building
DELETE = rm -rf         # Command to remove files
OUT ?= -o $(PROG)       # Compiler argument for output file
SOURCES = main.c mongoose.c net.c data_source.c   # Source code files
CFLAGS = -W -Wall -Wextra -g3 -ggdb -O0 -fno-omit-frame-pointer -I.                # Build options

# Database mode: SQLite (default) or CSV
# To use SQLite (default): make
# To use CSV: make CSV_MODE=1
ifeq ($(CSV_MODE),1)
  CFLAGS += -DCSV_MODE
else
  SOURCES += sqlite3.c
endif

ifeq ($(OS),Windows_NT)         # Windows settings. Assume MinGW compiler. To use VC: make CC=cl CFLAGS=/MD OUT=/Feprog.exe
  PROG = vvvv.exe            # Use .exe suffix for the binary
  CC = gcc                      # Use MinGW gcc compiler
  CFLAGS += -lws2_32            # Link against Winsock library
  DELETE = cmd /C del /Q /F /S  # Command prompt command to delete files
endif

all: $(PROG)
	$(RUN) $(PROG) $(ARGS)

$(PROG): $(SOURCES)
	$(CC) $(SOURCES) $(CFLAGS) $(CFLAGS_MONGOOSE) $(CFLAGS_EXTRA) $(OUT)

web_root/bundle.js:
	curl -s https://npm.reversehttp.com/preact,preact/hooks,htm/preact,preact-router -o $@

clean:
	$(DELETE) $(PROG) $(PACK) *.o *.obj *.exe *.dSYM