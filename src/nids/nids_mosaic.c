/*
 * nids_mosaic.c — NEXRAD NIDS Level-3 to PNG mosaic renderer
 *
 * Reads N0B (Base Reflectivity dual-pol) or N0H NIDS files for all
 * radar sites and composites them into a single georeferenced PNG mosaic.
 *
 * Build:
 *   gcc -O2 -o nids_mosaic nids_mosaic.c -lpng -lm -lbz2 -Wall
 *
 * Usage:
 *   nids_mosaic -d /LDM/radar/level3 -s radar_sites.csv \
 *               -p N0B -o /LDM/radar/mosaic/CONUS_N0B_latest.png [-v]
 *
 * NIDS Digital Radial Data Array (packet 0x0010):
 *   - Decompressed symbology block header (10 bytes)
 *   - Layer header (6 bytes)
 *   - Packet header (14 bytes): code, first_bin, n_bins, icen, jcen, scale, n_radials
 *   - n_radials × (6-byte header + n_bytes raw data)
 *   - Radial header: n_bytes(u16), start_az(i16 ×0.1°), delta_az(u16 ×0.1°)
 *   - Data: 1 byte per bin, raw encoded value
 *
 * N0B encoding: dBZ = value/2.0 - 32.0  (value 0,1 = below threshold)
 * N0H encoding: class = value/10         (value 0-9 = no data)
 *
 * Bin size: 250m (0.25 km) for both N0B and N0H dual-pol products
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include <time.h>
#include <dirent.h>
#include <sys/stat.h>
#include <bzlib.h>
#include <png.h>

/* ── Constants ─────────────────────────────────────────────────────────── */
#define MAX_SITES       200
#define MAX_RADIALS     720   /* N0B has 720 radials at 0.5° */
#define MAX_BINS        1840  /* N0B has 1840 bins */
#define PI              3.14159265358979323846
#define DEG2RAD         (PI / 180.0)
#define BIN_SIZE_KM     0.25  /* 250m for dual-pol products */
#define MAX_RANGE_KM    230.0 /* clip to 230km (avoid range folding artifacts) */

/* CONUS mosaic bounds */
#define LAT_MIN  20.0
#define LAT_MAX  52.0
#define LON_MIN -127.0
#define LON_MAX  -65.0

#define DEFAULT_WIDTH   5500
#define DEFAULT_HEIGHT  3000
#define MAX_AGE_SECS    720   /* 12 minutes — two volume scans */

/* ── NWS Standard Reflectivity Color Table ───────────────────────────────
 * 16 colors mapping dBZ ranges. Threshold at 5 dBZ (value 74 for N0B).
 * Colors match NWS standard radar display.
 */
typedef struct { uint8_t r, g, b, a; } RGBA;

/* Map N0B raw value (0-255) to RGBA.
 * dBZ = val/2 - 32  →  val = (dBZ+32)*2
 * Thresholds: <5dBZ=transparent, 5-9=lt blue, 10-14=blue, 15-19=dk green,
 *   20-24=green, 25-29=lt green, 30-34=yellow, 35-39=lt orange, 
 *   40-44=orange, 45-49=dk red, 50-54=red, 55-59=magenta, 60+=white
 */
static RGBA n0b_color(uint8_t val) {
    /* val 0,1 = below threshold / no data */
    if (val <= 1) return (RGBA){0,0,0,0};
    float dbz = val / 2.0f - 32.0f;
    if (dbz < 5.0f)  return (RGBA){0,0,0,0};
    if (dbz < 10.0f) return (RGBA){  4,233,231,180};
    if (dbz < 15.0f) return (RGBA){  1,159,244,190};
    if (dbz < 20.0f) return (RGBA){  3,  0,244,200};
    if (dbz < 25.0f) return (RGBA){  2,253,  2,210};
    if (dbz < 30.0f) return (RGBA){  1,192,  1,210};
    if (dbz < 35.0f) return (RGBA){ 52,125,  0,215};
    if (dbz < 40.0f) return (RGBA){253,191,  1,220};
    if (dbz < 45.0f) return (RGBA){253,116,  0,225};
    if (dbz < 50.0f) return (RGBA){253,  0,  0,230};
    if (dbz < 55.0f) return (RGBA){188,  0,  0,235};
    if (dbz < 60.0f) return (RGBA){148,  0,211,240};
    if (dbz < 65.0f) return (RGBA){253,144,253,245};
    return                  (RGBA){255,255,255,255};
}

/* Map N0H raw value to RGBA.
 * class = val/10: 1=ND,2=Bio,3=AP,4=IceCrystals,5=DrySnow,
 *                 6=WetSnow,7=LightRain,8=ModRain,9=HeavyRain,
 *                 10=BigDrops,11=Hail+Rain,14=GC/Unknown
 */
static RGBA n0h_color(uint8_t val) {
    int cls = val / 10;
    switch (cls) {
        case 0:  return (RGBA){0,0,0,0};        /* No data */
        case 1:  return (RGBA){0,0,0,0};        /* ND below threshold */
        case 2:  return (RGBA){ 99,207,  0,180}; /* Biological */
        case 3:  return (RGBA){255,165,  0,180}; /* Anomalous Propagation */
        case 4:  return (RGBA){150,200,255,180}; /* Ice Crystals */
        case 5:  return (RGBA){  0,200,255,200}; /* Dry Snow */
        case 6:  return (RGBA){  0,100,255,200}; /* Wet Snow */
        case 7:  return (RGBA){  2,253,  2,200}; /* Light Rain */
        case 8:  return (RGBA){253,191,  1,210}; /* Moderate Rain */
        case 9:  return (RGBA){253,  0,  0,220}; /* Heavy Rain */
        case 10: return (RGBA){253,144,253,230}; /* Big Drops */
        case 11: return (RGBA){255,255,255,240}; /* Hail+Rain */
        case 14: return (RGBA){100,100,100,150}; /* GC/Unknown */
        default: return (RGBA){0,0,0,0};
    }
}

/* ── Data structures ────────────────────────────────────────────────────── */
typedef struct {
    char   site_id[8];
    double lat, lon;
} RadarSite;

typedef struct {
    int     n_radials;
    int     n_bins;
    float   start_az[MAX_RADIALS];
    float   delta_az[MAX_RADIALS];
    uint8_t data[MAX_RADIALS][MAX_BINS];
} NidsData;

typedef struct {
    int      width, height;
    double   lat_min, lat_max, lon_min, lon_max;
    uint8_t *rgba;
} Canvas;

/* ── Utilities ───────────────────────────────────────────────────────────── */
static int verbose = 0;
#define LOG(fmt,...) do{if(verbose)fprintf(stderr,fmt"\n",##__VA_ARGS__);}while(0)
#define ERR(fmt,...) fprintf(stderr,"ERROR: "fmt"\n",##__VA_ARGS__)

static inline uint16_t u16be(const uint8_t *p){return((uint16_t)p[0]<<8)|p[1];}
static inline int16_t  s16be(const uint8_t *p){return(int16_t)u16be(p);}

/* ── Site loader ────────────────────────────────────────────────────────── */
static int load_sites(const char *csv, RadarSite *sites, int max) {
    FILE *f = fopen(csv, "r"); if(!f){ERR("Cannot open %s",csv);return -1;}
    char line[256]; int n=0; int hdr=1;
    while(fgets(line,sizeof(line),f) && n<max){
        if(hdr){hdr=0;continue;}
        char sid[8], name[64], elev[16];
        double lat,lon;
        if(sscanf(line,"%7[^,],%63[^,],%lf,%lf,%15s",sid,name,&lat,&lon,elev)>=4){
            memcpy(sites[n].site_id,sid,8);
            sites[n].lat=lat; sites[n].lon=lon; n++;
        }
    }
    fclose(f);
    LOG("Loaded %d sites",n);
    return n;
}

/* ── File finder ────────────────────────────────────────────────────────── */
static int find_nids(const char *base, const char *site, const char *prod,
                     char *out, size_t olen) {
    time_t now=time(NULL);
    char dates[2][16];
    struct tm *t=gmtime(&now);
    strftime(dates[0],16,"%Y%m%d",t);
    time_t y=now-86400;
    strftime(dates[1],16,"%Y%m%d",gmtime(&y));

    for(int di=0;di<2;di++){
        char dir[512];
        snprintf(dir,sizeof(dir),"%s/%s/%s/nids/%s",base,site,prod,dates[di]);
        DIR *d=opendir(dir); if(!d) continue;
        struct dirent *e;
        char best[256]=""; time_t bmt=0;
        while((e=readdir(d))){
            if(e->d_name[0]=='.') continue;
            if(!strstr(e->d_name,".nids")) continue;
            char fp[512]; snprintf(fp,sizeof(fp),"%s/%s",dir,e->d_name);
            struct stat st;
            if(stat(fp,&st)==0 && st.st_mtime>bmt){
                bmt=st.st_mtime;
                strncpy(best,e->d_name,sizeof(best)-1);
            }
        }
        closedir(d);
        if(best[0] && (now-bmt)<=MAX_AGE_SECS){
            snprintf(out,olen,"%s/%s",dir,best);
            return 1;
        }
    }
    return 0;
}

/* ── NIDS parser ─────────────────────────────────────────────────────────── */
static int parse_nids(const char *path, NidsData *nd) {
    FILE *f=fopen(path,"rb"); if(!f) return 0;
    fseek(f,0,SEEK_END); long fsz=ftell(f); rewind(f);
    uint8_t *raw=malloc(fsz);
    if(!raw||fread(raw,1,fsz,f)!=(size_t)fsz){free(raw);fclose(f);return 0;}
    fclose(f);

    /* Find bzip2 signature */
    int bz=-1;
    for(int i=0;i<fsz-3;i++){
        if(raw[i]=='B'&&raw[i+1]=='Z'&&raw[i+2]=='h'){bz=i;break;}
    }
    if(bz<0){ERR("No bzip2 in %s",path);free(raw);return 0;}

    /* Decompress */
    unsigned int dlen=2*1024*1024;
    /* N0B decompressed is ~434KB, N0H ~400KB — 2MB is safe */
    uint8_t *dec=malloc(dlen);
    if(!dec){free(raw);return 0;}
    int bret=BZ2_bzBuffToBuffDecompress((char*)dec,&dlen,
                                         (char*)(raw+bz),fsz-bz,0,0);
    free(raw);
    if(bret!=BZ_OK&&bret!=BZ_STREAM_END){
        ERR("bzip2 failed %d: %s",bret,path);
        free(dec);return 0;
    }

    /* Parse symbology block:
     * Offset  0: divider (s16) = -1
     * Offset  2: block_id (u16) = 1
     * Offset  4: block_len (u32)
     * Offset  8: n_layers (u16)
     * Offset 10: layer divider (s16) = -1
     * Offset 12: layer_len (u32)
     * Offset 16: packet code (u16) — expect 0x0010
     * Offset 18: first_bin (s16)
     * Offset 20: n_bins (u16)
     * Offset 22: i_center (s16)
     * Offset 24: j_center (s16)
     * Offset 26: scale (u16) — pixels/km display, ignore
     * Offset 28: n_radials (u16)
     * Offset 30: first radial data
     */
    if(dlen < 32){ERR("Too short: %s",path);free(dec);return 0;}

    uint16_t pcode = u16be(dec+16);
    if(pcode != 0x0010){
        ERR("Unexpected packet code 0x%04X in %s",pcode,path);
        free(dec);return 0;
    }

    uint16_t n_bins     = u16be(dec+20);
    uint16_t n_radials  = u16be(dec+28);

    if(n_radials==0||n_radials>MAX_RADIALS||n_bins==0||n_bins>MAX_BINS){
        ERR("Bad dimensions rad=%d bins=%d: %s",n_radials,n_bins,path);
        free(dec);return 0;
    }

    memset(nd,0,sizeof(*nd));
    nd->n_radials = n_radials;
    nd->n_bins    = n_bins;

    uint32_t pos=30;
    for(int r=0;r<n_radials;r++){
        if(pos+6 > dlen){ERR("Truncated at radial %d: %s",r,path);free(dec);return 0;}
        uint16_t nb  = u16be(dec+pos);   pos+=2;
        int16_t  saz = s16be(dec+pos);   pos+=2;
        uint16_t daz = u16be(dec+pos);   pos+=2;
        nd->start_az[r] = saz * 0.1f;
        nd->delta_az[r] = daz * 0.1f;
        if(pos+nb > dlen) nb = dlen-pos;
        uint32_t copy = (nb < MAX_BINS) ? nb : MAX_BINS;
        memcpy(nd->data[r], dec+pos, copy);
        pos += nb;
    }

    free(dec);
    return 1;
}

/* ── Canvas ──────────────────────────────────────────────────────────────── */
static Canvas *canvas_new(int w,int h,
                           double lamin,double lamax,
                           double lomin,double lomax){
    Canvas *c=malloc(sizeof(Canvas));
    c->width=w; c->height=h;
    c->lat_min=lamin; c->lat_max=lamax;
    c->lon_min=lomin; c->lon_max=lomax;
    c->rgba=calloc(w*h*4,1);
    return c;
}
static void canvas_free(Canvas *c){free(c->rgba);free(c);}

static inline void ll2px(const Canvas *c,double lat,double lon,int *px,int *py){
    *px=(int)((lon-c->lon_min)/(c->lon_max-c->lon_min)*c->width);
    *py=(int)((c->lat_max-lat)/(c->lat_max-c->lat_min)*c->height);
}

/* Paint one site onto canvas */
static void paint_site(Canvas *c, const RadarSite *s, const NidsData *nd,
                        int is_n0h) {
    double km_per_lat = 111.32;
    double km_per_lon = km_per_lat * cos(s->lat * DEG2RAD);

    for(int r=0; r<nd->n_radials; r++){
        float az0 = nd->start_az[r];
        float daz = nd->delta_az[r];

        /* Sample azimuth at multiple sub-steps for smooth fill */
        int nsub = (int)(daz / 0.25f) + 2;
        if(nsub<2) nsub=2;
        if(nsub>8) nsub=8;

        for(int b=0; b<nd->n_bins; b++){
            uint8_t val = nd->data[r][b];
            if(val <= 1) continue;

            RGBA col = is_n0h ? n0h_color(val) : n0b_color(val);
            if(col.a == 0) continue;

            double rng_km = (b + 0.5) * BIN_SIZE_KM;
            if(rng_km > MAX_RANGE_KM) break;

            for(int sub=0; sub<nsub; sub++){
                float az_deg = az0 + daz * sub / (float)(nsub-1);
                double az_rad = az_deg * DEG2RAD;
                double dx_km  =  rng_km * sin(az_rad);
                double dy_km  =  rng_km * cos(az_rad);

                double pt_lat = s->lat + dy_km / km_per_lat;
                double pt_lon = s->lon + dx_km / km_per_lon;

                if(pt_lat < c->lat_min || pt_lat > c->lat_max) continue;
                if(pt_lon < c->lon_min || pt_lon > c->lon_max) continue;

                int px, py;
                ll2px(c, pt_lat, pt_lon, &px, &py);
                if(px<0||px>=c->width||py<0||py>=c->height) continue;

                uint8_t *p = c->rgba + (py*c->width+px)*4;
                /* Max-value compositing for reflectivity */
                if(p[3]==0 || val > p[3]){
                    p[0]=col.r; p[1]=col.g; p[2]=col.b; p[3]=col.a;
                }
            }
        }
    }
}

/* ── PNG writer ──────────────────────────────────────────────────────────── */
static int write_png(const Canvas *c, const char *path,
                     const char *prod, time_t gen) {
    FILE *f=fopen(path,"wb"); if(!f){ERR("Cannot write %s",path);return 0;}
    png_structp png=png_create_write_struct(PNG_LIBPNG_VER_STRING,0,0,0);
    png_infop   info=png_create_info_struct(png);
    if(setjmp(png_jmpbuf(png))){
        png_destroy_write_struct(&png,&info);fclose(f);return 0;
    }
    png_init_io(png,f);
    png_set_IHDR(png,info,c->width,c->height,8,PNG_COLOR_TYPE_RGBA,
                 PNG_INTERLACE_NONE,PNG_COMPRESSION_TYPE_DEFAULT,
                 PNG_FILTER_TYPE_DEFAULT);

    /* Embed georef metadata as text chunks */
    char s_lamin[32],s_lamax[32],s_lomin[32],s_lomax[32],s_gen[64];
    snprintf(s_lamin,32,"%.4f",c->lat_min); snprintf(s_lamax,32,"%.4f",c->lat_max);
    snprintf(s_lomin,32,"%.4f",c->lon_min); snprintf(s_lomax,32,"%.4f",c->lon_max);
    struct tm *tg=gmtime(&gen);
    strftime(s_gen,64,"%Y-%m-%dT%H:%M:%SZ",tg);

    png_text txt[6]; memset(txt,0,sizeof(txt));
    int tc=0;
    #define T(k,v) txt[tc].compression=PNG_TEXT_COMPRESSION_NONE;\
                   txt[tc].key=(char*)(k);txt[tc].text=(char*)(v);tc++
    T("lat_min",s_lamin); T("lat_max",s_lamax);
    T("lon_min",s_lomin); T("lon_max",s_lomax);
    T("generated",s_gen); T("product",(char*)prod);
    #undef T
    png_set_text(png,info,txt,tc);
    png_write_info(png,info);

    for(int y=0;y<c->height;y++)
        png_write_row(png,c->rgba+y*c->width*4);
    png_write_end(png,NULL);
    png_destroy_write_struct(&png,&info);
    fclose(f);
    return 1;
}

/* ── JSON sidecar ────────────────────────────────────────────────────────── */
static void write_json(const char *path, const char *prod, int n_sites,
                       time_t gen, const Canvas *c) {
    FILE *f=fopen(path,"w"); if(!f) return;
    struct tm *tg=gmtime(&gen); char gs[64];
    strftime(gs,64,"%Y-%m-%dT%H:%M:%SZ",tg);
    fprintf(f,"{\n  \"product\":\"%s\",\n  \"generated\":\"%s\",\n"
              "  \"n_sites\":%d,\n  \"bounds\":{"
              "\"lat_min\":%.4f,\"lat_max\":%.4f,"
              "\"lon_min\":%.4f,\"lon_max\":%.4f}\n}\n",
            prod,gs,n_sites,c->lat_min,c->lat_max,c->lon_min,c->lon_max);
    fclose(f);
}

/* ── Main ────────────────────────────────────────────────────────────────── */
static void usage(const char *p){
    fprintf(stderr,
        "Usage: %s -d level3_dir -s sites.csv -p PRODUCT -o output.png\n"
        "  -d DIR    /LDM/radar/level3\n"
        "  -s CSV    radar_sites.csv\n"
        "  -p PROD   N0B or N0H\n"
        "  -o PNG    output file\n"
        "  -w WIDTH  [%d]\n  -h HEIGHT [%d]\n  -v verbose\n",
        p,DEFAULT_WIDTH,DEFAULT_HEIGHT);
}

int main(int argc, char **argv){
    const char *l3dir=NULL, *csv=NULL, *prod="N0B", *outpng=NULL;
    int width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT;

    for(int i=1;i<argc;i++){
        if(!strcmp(argv[i],"-d")&&i+1<argc) l3dir=argv[++i];
        else if(!strcmp(argv[i],"-s")&&i+1<argc) csv=argv[++i];
        else if(!strcmp(argv[i],"-p")&&i+1<argc) prod=argv[++i];
        else if(!strcmp(argv[i],"-o")&&i+1<argc) outpng=argv[++i];
        else if(!strcmp(argv[i],"-w")&&i+1<argc) width=atoi(argv[++i]);
        else if(!strcmp(argv[i],"-h")&&i+1<argc) height=atoi(argv[++i]);
        else if(!strcmp(argv[i],"-v")) verbose=1;
        else{usage(argv[0]);return 1;}
    }
    if(!l3dir||!csv||!outpng){usage(argv[0]);return 1;}

    int is_n0h = (strcmp(prod,"N0H")==0);

    RadarSite sites[MAX_SITES];
    int nsites=load_sites(csv,sites,MAX_SITES);
    if(nsites<=0) return 1;

    Canvas *canvas=canvas_new(width,height,LAT_MIN,LAT_MAX,LON_MIN,LON_MAX);

    int n_rendered=0;
    char npath[512];
    NidsData nd;

    for(int i=0;i<nsites;i++){
        /* CONUS only */
        if(sites[i].lat<LAT_MIN||sites[i].lat>LAT_MAX) continue;
        if(sites[i].lon<LON_MIN||sites[i].lon>LON_MAX) continue;

        if(!find_nids(l3dir,sites[i].site_id,prod,npath,sizeof(npath))){
            LOG("No recent %s for %s",prod,sites[i].site_id);
            continue;
        }
        LOG("Rendering %s: %s",sites[i].site_id,npath);
        if(!parse_nids(npath,&nd)){
            LOG("Parse failed: %s",npath);
            continue;
        }
        paint_site(canvas,&sites[i],&nd,is_n0h);
        n_rendered++;
    }

    fprintf(stderr,"Rendered %d/%d CONUS sites → %s\n",n_rendered,nsites,outpng);

    time_t now=time(NULL);
    if(!write_png(canvas,outpng,prod,now)){canvas_free(canvas);return 1;}

    /* JSON sidecar */
    char jpath[512];
    strncpy(jpath,outpng,sizeof(jpath)-6);
    char *dot=strrchr(jpath,'.');
    if(dot) strcpy(dot,".json"); else strcat(jpath,".json");
    write_json(jpath,prod,n_rendered,now,canvas);

    canvas_free(canvas);
    return 0;
}
