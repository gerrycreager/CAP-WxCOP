/*
 * nids2geopng.c — NEXRAD NIDS Level-3 to georeferenced PNG renderer
 *
 * Drop-in replacement for nids2geopng.py. Reads a single NIDS file and
 * writes a PNG + .pgw world file (ESRI/MapServer compatible).
 *
 * Build:
 *   gcc -O2 -o nids2geopng nids2geopng.c -lpng -lm -lbz2 -lz -Wall
 *
 * Usage (matches existing pqact EXEC signature):
 *   nids2geopng -i input.nids -o output.png -s SITE -p PRODUCT
 *
 * Output:
 *   output.png  — RGBA PNG, georeferenced to WGS84
 *   output.pgw  — ESRI world file for MapServer/GDAL
 *
 * Supported formats:
 *   - Direct bzip2 (legacy NIDS)
 *   - Pure zlib (SDUS5x products)
 *   - Nested zlib+bzip2 (SDUS53 super-res products)
 *
 * Products: N0B, N0H, NCR, N0Q and any other packet-0x0010 product
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include <sys/stat.h>
#include <bzlib.h>
#include <zlib.h>
#include <png.h>

/* ── Constants ───────────────────────────────────────────────────────── */
#define MAX_RADIALS  720
#define MAX_BINS     1840
#define BIN_SIZE_KM  0.25
#define MAX_RANGE_KM 230.0
#define PI           3.14159265358979323846
#define DEG2RAD      (PI/180.0)
#define IMG_SIZE     1024   /* output PNG size in pixels */

/* ── Color tables ────────────────────────────────────────────────────── */
typedef struct { uint8_t r,g,b,a; } RGBA;

/* N0B/N0Q Base Reflectivity: dBZ = val/2 - 32 */
static RGBA refl_color(uint8_t val) {
    if(val<=1) return (RGBA){0,0,0,0};
    float dbz = val/2.0f - 32.0f;
    if(dbz< 5.0f) return (RGBA){0,0,0,0};
    if(dbz<10.0f) return (RGBA){  4,233,231,180};
    if(dbz<15.0f) return (RGBA){  1,159,244,190};
    if(dbz<20.0f) return (RGBA){  3,  0,244,200};
    if(dbz<25.0f) return (RGBA){  2,253,  2,210};
    if(dbz<30.0f) return (RGBA){  1,192,  1,210};
    if(dbz<35.0f) return (RGBA){ 52,125,  0,215};
    if(dbz<40.0f) return (RGBA){253,191,  1,220};
    if(dbz<45.0f) return (RGBA){253,116,  0,225};
    if(dbz<50.0f) return (RGBA){253,  0,  0,230};
    if(dbz<55.0f) return (RGBA){188,  0,  0,235};
    if(dbz<60.0f) return (RGBA){148,  0,211,240};
    if(dbz<65.0f) return (RGBA){253,144,253,245};
    return              (RGBA){255,255,255,255};
}

/* N0H Hydrometeor Classification: class = val/10 */
static RGBA hydro_color(uint8_t val) {
    int cls = val/10;
    switch(cls) {
        case 0: case 1: return (RGBA){0,0,0,0};
        case 2:  return (RGBA){ 99,207,  0,180};
        case 3:  return (RGBA){255,165,  0,180};
        case 4:  return (RGBA){150,200,255,180};
        case 5:  return (RGBA){  0,200,255,200};
        case 6:  return (RGBA){  0,100,255,200};
        case 7:  return (RGBA){  2,253,  2,200};
        case 8:  return (RGBA){253,191,  1,210};
        case 9:  return (RGBA){253,  0,  0,220};
        case 10: return (RGBA){253,144,253,230};
        case 11: return (RGBA){255,255,255,240};
        case 14: return (RGBA){100,100,100,150};
        default: return (RGBA){0,0,0,0};
    }
}

/* NCR Composite Reflectivity: same encoding as N0B */
/* N0G Velocity: val=0 range folded, val=1 below threshold, else velocity */
static RGBA vel_color(uint8_t val) {
    if(val<=1) return (RGBA){0,0,0,0};
    /* velocity = (val - 129) * 0.5 m/s (approximately) */
    float v = (val - 129) * 0.5f;
    if(v < -30.0f) return (RGBA){  0,  0,180,220};
    if(v < -20.0f) return (RGBA){  0, 70,255,220};
    if(v < -10.0f) return (RGBA){  0,180,255,210};
    if(v <  -2.0f) return (RGBA){100,220,255,200};
    if(v <   2.0f) return (RGBA){  0,  0,  0,  0};  /* near-zero = transparent */
    if(v <  10.0f) return (RGBA){255,220,100,200};
    if(v <  20.0f) return (RGBA){255,150,  0,210};
    if(v <  30.0f) return (RGBA){255, 50,  0,220};
    return              (RGBA){180,  0,  0,230};
}

/* ── Inline functions ────────────────────────────────────────────────── */
static inline uint16_t u16be(const uint8_t *p){return((uint16_t)p[0]<<8)|p[1];}
static inline int16_t  s16be(const uint8_t *p){return(int16_t)u16be(p);}

/* ── NIDS data structure ─────────────────────────────────────────────── */
typedef struct {
    int     n_radials, n_bins;
    float   start_az[MAX_RADIALS];
    float   delta_az[MAX_RADIALS];
    uint8_t data[MAX_RADIALS][MAX_BINS];
} NidsData;

/* ── Radar site table (lat/lon for common sites) ─────────────────────── */
/* Loaded from CSV at runtime if available, else uses embedded fallback */
typedef struct { char id[8]; double lat, lon; } SiteCoord;

static int load_sites_csv(const char *path, SiteCoord *sites, int max) {
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    char line[256]; int n=0; int hdr=1;
    while (fgets(line, sizeof(line), f) && n < max) {
        if (hdr) { hdr=0; continue; }
        char sid[8], name[64], elev[16];
        double lat, lon;
        if (sscanf(line, "%7[^,],%63[^,],%lf,%lf,%15s", sid, name, &lat, &lon, elev) >= 4) {
            memcpy(sites[n].id, sid, 8);
            sites[n].lat = lat;
            sites[n].lon = lon;
            n++;
        }
    }
    fclose(f);
    return n;
}

static int find_site(const SiteCoord *sites, int n, const char *id,
                     double *lat, double *lon) {
    for (int i = 0; i < n; i++) {
        if (strcasecmp(sites[i].id, id) == 0) {
            *lat = sites[i].lat;
            *lon = sites[i].lon;
            return 1;
        }
    }
    return 0;
}

/* ── NIDS parser ─────────────────────────────────────────────────────── */
static int parse_nids(const char *path, NidsData *nd) {
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    fseek(f, 0, SEEK_END);
    long fsz = ftell(f);
    rewind(f);
    uint8_t *raw = malloc(fsz);
    if (!raw || fread(raw, 1, fsz, f) != (size_t)fsz) {
        free(raw); fclose(f); return 0;
    }
    fclose(f);

    /* Find compression signature */
    int bz = -1;
    for (int i = 0; i < fsz-3; i++) {
        if (raw[i]=='B' && raw[i+1]=='Z' && raw[i+2]=='h') { bz=i; break; }
    }
    if (bz < 0) {
        for (int i = 0; i < fsz-1; i++) {
            if (raw[i]==0x78 && (raw[i+1]==0x9c || raw[i+1]==0xda ||
                                  raw[i+1]==0x01 || raw[i+1]==0x5e)) {
                bz = i; break;
            }
        }
    }
    if (bz < 0) { free(raw); return 0; }

    /* Decompress: bzip2 / zlib / nested zlib+bzip2 */
    unsigned long dlen = 8*1024*1024;
    uint8_t *dec = malloc(dlen);
    if (!dec) { free(raw); return 0; }
    int ok = 0;

    /* Format 1: direct bzip2 */
    if (!ok && raw[bz]=='B') {
        unsigned int bl = (unsigned int)dlen;
        int r = BZ2_bzBuffToBuffDecompress((char*)dec, &bl,
                                            (char*)(raw+bz), fsz-bz, 0, 0);
        if (r==BZ_OK || r==BZ_STREAM_END) { dlen=bl; ok=1; }
    }

    /* Format 2: zlib outer + optional bzip2 inner */
    if (!ok && raw[bz]==0x78) {
        unsigned long il = 512*1024;
        uint8_t *inner = malloc(il);
        if (inner) {
            z_stream zs; memset(&zs, 0, sizeof(zs));
            zs.next_in  = (Bytef*)(raw+bz);
            zs.avail_in = (uInt)(fsz-bz);
            if (inflateInit(&zs) == Z_OK) {
                il = 0;
                uint8_t chunk[65536]; int zr;
                do {
                    zs.next_out  = (Bytef*)chunk;
                    zs.avail_out = sizeof(chunk);
                    zr = inflate(&zs, Z_NO_FLUSH);
                    unsigned got = sizeof(chunk) - zs.avail_out;
                    if (il + got <= 512*1024) {
                        memcpy(inner+il, chunk, got); il += got;
                    }
                } while (zr == Z_OK);
                long zc = (fsz-bz) - (long)zs.avail_in;
                inflateEnd(&zs);

                /* Look for nested bzip2 */
                int bz2i = -1;
                for (int i = 0; i < (int)il-3; i++)
                    if (inner[i]=='B' && inner[i+1]=='Z' && inner[i+2]=='h')
                        { bz2i=i; break; }

                if (bz2i >= 0) {
                    long ts = bz+zc, it = il-bz2i, ft = fsz-ts;
                    uint8_t *bb = malloc(it+ft);
                    if (bb) {
                        memcpy(bb,    inner+bz2i, it);
                        memcpy(bb+it, raw+ts,     ft);
                        unsigned int bl = (unsigned int)dlen;
                        int r = BZ2_bzBuffToBuffDecompress(
                                    (char*)dec, &bl, (char*)bb,
                                    (unsigned int)(it+ft), 0, 0);
                        free(bb);
                        if (r==BZ_OK || r==BZ_STREAM_END) { dlen=bl; ok=1; }
                    }
                } else if (il > 0 && il < dlen) {
                    memcpy(dec, inner, il); dlen=il; ok=1;
                }
            }
            free(inner);
        }
    }
    free(raw);
    if (!ok || dlen < 32) { free(dec); return 0; }

    /* Parse symbology block */
    if (u16be(dec+16) != 0x0010) { free(dec); return 0; }
    uint16_t n_bins    = u16be(dec+20);
    uint16_t n_radials = u16be(dec+28);
    if (!n_radials || n_radials > MAX_RADIALS ||
        !n_bins    || n_bins    > MAX_BINS) {
        free(dec); return 0;
    }

    memset(nd, 0, sizeof(*nd));
    nd->n_radials = n_radials;
    nd->n_bins    = n_bins;

    uint32_t pos = 30;
    for (int r = 0; r < n_radials; r++) {
        if (pos+6 > dlen) { free(dec); return 0; }
        uint16_t nb  = u16be(dec+pos); pos+=2;
        int16_t  saz = s16be(dec+pos); pos+=2;
        uint16_t daz = u16be(dec+pos); pos+=2;
        nd->start_az[r] = saz * 0.1f;
        nd->delta_az[r] = daz * 0.1f;
        if (pos+nb > dlen) nb = dlen-pos;
        uint32_t cp = (nb < MAX_BINS) ? nb : MAX_BINS;
        memcpy(nd->data[r], dec+pos, cp);
        pos += nb;
    }
    free(dec);
    return 1;
}

/* ── PNG + world file writer ─────────────────────────────────────────── */
static int write_png_pgw(const char *png_path,
                         const uint8_t *rgba, int size,
                         double lat_min, double lat_max,
                         double lon_min, double lon_max) {
    /* PNG */
    FILE *f = fopen(png_path, "wb");
    if (!f) { fprintf(stderr, "Cannot write %s\n", png_path); return 0; }
    png_structp png  = png_create_write_struct(PNG_LIBPNG_VER_STRING,0,0,0);
    png_infop   info = png_create_info_struct(png);
    if (setjmp(png_jmpbuf(png))) {
        png_destroy_write_struct(&png, &info); fclose(f); return 0;
    }
    png_init_io(png, f);
    png_set_IHDR(png, info, size, size, 8, PNG_COLOR_TYPE_RGBA,
                 PNG_INTERLACE_NONE, PNG_COMPRESSION_TYPE_DEFAULT,
                 PNG_FILTER_TYPE_DEFAULT);
    png_write_info(png, info);
    for (int y = 0; y < size; y++)
        png_write_row(png, (png_bytep)(rgba + y*size*4));
    png_write_end(png, NULL);
    png_destroy_write_struct(&png, &info);
    fclose(f);

    /* World file (.pgw) */
    char pgw_path[512];
    strncpy(pgw_path, png_path, sizeof(pgw_path)-5);
    char *dot = strrchr(pgw_path, '.');
    if (dot) strcpy(dot, ".pgw"); else strcat(pgw_path, ".pgw");

    double px_lon = (lon_max - lon_min) / size;
    double px_lat = (lat_max - lat_min) / size;
    double ul_lon = lon_min + px_lon * 0.5;
    double ul_lat = lat_max - px_lat * 0.5;

    FILE *pgw = fopen(pgw_path, "w");
    if (pgw) {
        fprintf(pgw, "%.10f\n",  px_lon);   /* pixel size X */
        fprintf(pgw, "0.0\n");               /* rotation X */
        fprintf(pgw, "0.0\n");               /* rotation Y */
        fprintf(pgw, "%.10f\n", -px_lat);   /* pixel size Y (negative) */
        fprintf(pgw, "%.10f\n",  ul_lon);   /* upper-left X (lon) */
        fprintf(pgw, "%.10f\n",  ul_lat);   /* upper-left Y (lat) */
        fclose(pgw);
    }
    return 1;
}

/* ── Main ────────────────────────────────────────────────────────────── */
static void usage(const char *prog) {
    fprintf(stderr,
        "Usage: %s -i input.nids -o output.png -s SITE -p PRODUCT\n"
        "  -i FILE    Input NIDS file\n"
        "  -o FILE    Output PNG file (also writes .pgw world file)\n"
        "  -s SITE    3-letter site ID (e.g. FTG)\n"
        "  -p PROD    Product code (N0B, N0H, NCR, N0Q, etc.)\n"
        "  -c CSV     Radar sites CSV (default: /home/ldm/etc/radar_sites.csv)\n"
        "  -r KM      Max range km [%.0f]\n"
        "  -z SIZE    Output PNG size [%d]\n",
        prog, MAX_RANGE_KM, IMG_SIZE);
}

int main(int argc, char **argv) {
    const char *infile  = NULL;
    const char *outfile = NULL;
    const char *site_id = NULL;
    const char *product = NULL;
    const char *csv     = "/home/ldm/etc/radar_sites.csv";
    double max_range    = MAX_RANGE_KM;
    int    img_size     = IMG_SIZE;

    for (int i = 1; i < argc; i++) {
        if      (!strcmp(argv[i],"-i") && i+1<argc) infile   = argv[++i];
        else if (!strcmp(argv[i],"-o") && i+1<argc) outfile  = argv[++i];
        else if (!strcmp(argv[i],"-s") && i+1<argc) site_id  = argv[++i];
        else if (!strcmp(argv[i],"-p") && i+1<argc) product  = argv[++i];
        else if (!strcmp(argv[i],"-c") && i+1<argc) csv      = argv[++i];
        else if (!strcmp(argv[i],"-r") && i+1<argc) max_range= atof(argv[++i]);
        else if (!strcmp(argv[i],"-z") && i+1<argc) img_size = atoi(argv[++i]);
        else { usage(argv[0]); return 1; }
    }
    if (!infile || !outfile || !site_id || !product) {
        usage(argv[0]); return 1;
    }

    /* Load site coordinates */
    SiteCoord sites[256];
    int n_sites = load_sites_csv(csv, sites, 256);
    double site_lat = 0, site_lon = 0;
    if (!find_site(sites, n_sites, site_id, &site_lat, &site_lon)) {
        fprintf(stderr, "ERROR: Site %s not found in %s\n", site_id, csv);
        return 1;
    }

    /* Parse NIDS */
    NidsData nd;
    if (!parse_nids(infile, &nd)) {
        fprintf(stderr, "ERROR: Parse failed: %s\n", infile);
        return 1;
    }

    /* Determine color function */
    int is_n0h = (strncasecmp(product, "N0H", 3) == 0);
    int is_vel = (strncasecmp(product, "N0G", 3) == 0 ||
                  strncasecmp(product, "N0V", 3) == 0);

    /* Compute bounds */
    double km_lat = 111.32;
    double km_lon = km_lat * cos(site_lat * DEG2RAD);
    double lat_off = max_range / km_lat;
    double lon_off = max_range / km_lon;
    double lat_min = site_lat - lat_off;
    double lat_max = site_lat + lat_off;
    double lon_min = site_lon - lon_off;
    double lon_max = site_lon + lon_off;

    /* Allocate canvas */
    uint8_t *rgba = calloc(img_size * img_size * 4, 1);
    if (!rgba) { fprintf(stderr, "ERROR: Out of memory\n"); return 1; }

    /* Render */
    for (int r = 0; r < nd.n_radials; r++) {
        float az0 = nd.start_az[r];
        float daz = nd.delta_az[r];
        int nsub = (int)(daz / 0.25f) + 2;
        if (nsub < 2) nsub = 2;
        if (nsub > 8) nsub = 8;

        for (int b = 0; b < nd.n_bins; b++) {
            uint8_t val = nd.data[r][b];
            if (val <= 1) continue;

            RGBA col = is_n0h ? hydro_color(val) :
                       is_vel ? vel_color(val)   :
                                refl_color(val);
            if (col.a == 0) continue;

            double rng = (b + 0.5) * BIN_SIZE_KM;
            if (rng > max_range) break;

            for (int s = 0; s < nsub; s++) {
                float az  = az0 + daz * s / (float)(nsub-1);
                double ar = az * DEG2RAD;
                double dx = rng * sin(ar);
                double dy = rng * cos(ar);
                double pt_lat = site_lat + dy / km_lat;
                double pt_lon = site_lon + dx / km_lon;

                if (pt_lat < lat_min || pt_lat > lat_max) continue;
                if (pt_lon < lon_min || pt_lon > lon_max) continue;

                int px = (int)((pt_lon-lon_min)/(lon_max-lon_min) * img_size);
                int py = (int)((lat_max-pt_lat)/(lat_max-lat_min) * img_size);
                if (px<0||px>=img_size||py<0||py>=img_size) continue;

                uint8_t *p = rgba + (py*img_size+px)*4;
                if (p[3]==0 || val > p[3]) {
                    p[0]=col.r; p[1]=col.g; p[2]=col.b; p[3]=col.a;
                }
            }
        }
    }

    /* Write output — create directory if needed */
    char outdir[512];
    strncpy(outdir, outfile, sizeof(outdir)-1);
    char *slash = strrchr(outdir, '/');
    if (slash) {
        *slash = '\0';
        /* Create directory tree (mkdir -p) */
        for (char *p = outdir+1; *p; p++) {
            if (*p == '/') {
                *p = '\0';
                mkdir(outdir, 0775);
                *p = '/';
            }
        }
        mkdir(outdir, 0775);
    }
    if (!write_png_pgw(outfile, rgba, img_size,
                       lat_min, lat_max, lon_min, lon_max)) {
        free(rgba); return 1;
    }
    free(rgba);
    return 0;
}
