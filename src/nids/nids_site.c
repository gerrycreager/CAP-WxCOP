/*
 * nids_site.c — Render a single NEXRAD NIDS file to a georeferenced PNG
 *
 * Build:
 *   gcc -O2 -o nids_site nids_site.c -lpng -lm -lbz2 -Wall
 *
 * Usage:
 *   nids_site -i input.nids -o output.png -la <lat> -lo <lon> [-p N0B|N0H] [-v]
 *
 * Output: RGBA PNG covering the radar's coverage area.
 * Bounds written to stdout as JSON: {"lat_min":...,"lat_max":...,...}
 *
 * N0B: dBZ = value/2 - 32, 720 radials × 0.5°, 1840 bins × 0.25km
 * N0H: class = value/10,  360 radials × 1.0°, 1200 bins × 0.25km
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include <bzlib.h>
#include <zlib.h>
#include <png.h>

#define MAX_RADIALS  720
#define MAX_BINS     1840
#define BIN_SIZE_KM  0.25
#define MAX_RANGE_KM 230.0
#define PI           3.14159265358979323846
#define DEG2RAD      (PI/180.0)

typedef struct { uint8_t r,g,b,a; } RGBA;

static inline uint16_t u16be(const uint8_t *p){return((uint16_t)p[0]<<8)|p[1];}
static inline int16_t  s16be(const uint8_t *p){return(int16_t)u16be(p);}

static RGBA n0b_color(uint8_t val){
    if(val<=1) return (RGBA){0,0,0,0};
    float dbz=val/2.0f-32.0f;
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

static RGBA n0h_color(uint8_t val){
    int cls=val/10;
    switch(cls){
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

typedef struct {
    int     n_radials, n_bins;
    float   start_az[MAX_RADIALS];
    float   delta_az[MAX_RADIALS];
    uint8_t data[MAX_RADIALS][MAX_BINS];
} NidsData;

static int parse_nids(const char *path, NidsData *nd){
    FILE *f=fopen(path,"rb"); if(!f) return 0;
    fseek(f,0,SEEK_END); long fsz=ftell(f); rewind(f);
    uint8_t *raw=malloc(fsz);
    if(!raw||fread(raw,1,fsz,f)!=(size_t)fsz){free(raw);fclose(f);return 0;}
    fclose(f);
    int bz=-1;
    /* Find bzip2 signature first */
    for(int i=0;i<fsz-3;i++){
        if(raw[i]=='B'&&raw[i+1]=='Z'&&raw[i+2]=='h'){bz=i;break;}
    }
    /* Fall back to zlib magic if no bzip2 */
    if(bz<0){
        for(int i=0;i<fsz-1;i++){
            if(raw[i]==0x78&&(raw[i+1]==0x9c||raw[i+1]==0xda||
                              raw[i+1]==0x01||raw[i+1]==0x5e)){
                bz=i;break;
            }
        }
    }
    if(bz<0){free(raw);return 0;}

    /* Allocate decompression buffer — 8MB for super-res products */
    unsigned long dlen=8*1024*1024;
    uint8_t *dec=malloc(dlen);
    if(!dec){free(raw);return 0;}
    int decomp_ok=0;

    /* Format 1: direct bzip2 */
    if(!decomp_ok && raw[bz]=='B'){
        unsigned int blen=(unsigned int)dlen;
        int bret=BZ2_bzBuffToBuffDecompress((char*)dec,&blen,
                                             (char*)(raw+bz),fsz-bz,0,0);
        if(bret==BZ_OK||bret==BZ_STREAM_END){dlen=blen;decomp_ok=1;}
    }

    /* Format 2: zlib outer wrapping bzip2 inner (nested compression)
     * e.g. SDUS53 products: zlib(msg_header+PDB+BZh_start) || bzip2_tail
     */
    if(!decomp_ok && raw[bz]==0x78){
        /* Decompress zlib outer */
        unsigned long inner_len=512*1024;
        uint8_t *inner=malloc(inner_len);
        if(inner){
            z_stream zs; memset(&zs,0,sizeof(zs));
            zs.next_in =(Bytef*)(raw+bz);
            zs.avail_in=(uInt)(fsz-bz);
            if(inflateInit(&zs)==Z_OK){
                inner_len=0;
                uint8_t chunk[65536];
                int zr;
                do{
                    zs.next_out=(Bytef*)chunk;
                    zs.avail_out=sizeof(chunk);
                    zr=inflate(&zs,Z_NO_FLUSH);
                    unsigned got=sizeof(chunk)-zs.avail_out;
                    if(inner_len+got<=512*1024){
                        memcpy(inner+inner_len,chunk,got);
                        inner_len+=got;
                    }
                }while(zr==Z_OK);
                long zlib_consumed=(fsz-bz)-(long)zs.avail_in;
                inflateEnd(&zs);

                /* Find BZh inside decompressed inner buffer */
                int bz2_inner=-1;
                for(int i=0;i<(int)inner_len-3;i++){
                    if(inner[i]=='B'&&inner[i+1]=='Z'&&inner[i+2]=='h'){
                        bz2_inner=i;break;
                    }
                }

                if(bz2_inner>=0){
                    /* Assemble: inner[bz2_inner:] + file bytes after zlib stream */
                    long tail_start=bz+zlib_consumed;
                    long inner_tail=inner_len-bz2_inner;
                    long file_tail =fsz-tail_start;
                    long total=inner_tail+file_tail;
                    uint8_t *bzbuf=malloc(total);
                    if(bzbuf){
                        memcpy(bzbuf,            inner+bz2_inner,inner_tail);
                        memcpy(bzbuf+inner_tail, raw+tail_start, file_tail);
                        unsigned int blen2=(unsigned int)dlen;
                        int bret2=BZ2_bzBuffToBuffDecompress(
                                      (char*)dec,&blen2,
                                      (char*)bzbuf,(unsigned int)total,0,0);
                        free(bzbuf);
                        if(bret2==BZ_OK||bret2==BZ_STREAM_END){
                            dlen=blen2;decomp_ok=1;
                        }
                    }
                } else {
                    /* Pure zlib (no nested bzip2) */
                    if(inner_len>0&&inner_len<dlen){
                        memcpy(dec,inner,inner_len);
                        dlen=inner_len;decomp_ok=1;
                    }
                }
            }
            free(inner);
        }
    }

    free(raw);
    if(!decomp_ok){fprintf(stderr,"ERROR: Decompress failed: %s\n",path);free(dec);return 0;}
    if(dlen<32){free(dec);return 0;}
    if(u16be(dec+16)!=0x0010){free(dec);return 0;}
    uint16_t n_bins    =u16be(dec+20);
    uint16_t n_radials =u16be(dec+28);
    if(!n_radials||n_radials>MAX_RADIALS||!n_bins||n_bins>MAX_BINS){
        free(dec);return 0;
    }
    memset(nd,0,sizeof(*nd));
    nd->n_radials=n_radials; nd->n_bins=n_bins;
    uint32_t pos=30;
    for(int r=0;r<n_radials;r++){
        if(pos+6>dlen){free(dec);return 0;}
        uint16_t nb =u16be(dec+pos); pos+=2;
        int16_t  saz=s16be(dec+pos); pos+=2;
        uint16_t daz=u16be(dec+pos); pos+=2;
        nd->start_az[r]=saz*0.1f;
        nd->delta_az[r]=daz*0.1f;
        if(pos+nb>dlen) nb=dlen-pos;
        uint32_t cp=(nb<MAX_BINS)?nb:MAX_BINS;
        memcpy(nd->data[r],dec+pos,cp);
        pos+=nb;
    }
    free(dec);
    return 1;
}

int main(int argc, char **argv){
    const char *infile=NULL, *outfile=NULL, *prod="N0B";
    double site_lat=0, site_lon=0;
    int verbose=0;
    int px_size=1024;  /* output image size */

    for(int i=1;i<argc;i++){
        if(!strcmp(argv[i],"-i")&&i+1<argc) infile=argv[++i];
        else if(!strcmp(argv[i],"-o")&&i+1<argc) outfile=argv[++i];
        else if(!strcmp(argv[i],"-la")&&i+1<argc) site_lat=atof(argv[++i]);
        else if(!strcmp(argv[i],"-lo")&&i+1<argc) site_lon=atof(argv[++i]);
        else if(!strcmp(argv[i],"-p")&&i+1<argc) prod=argv[++i];
        else if(!strcmp(argv[i],"-s")&&i+1<argc) px_size=atoi(argv[++i]);
        else if(!strcmp(argv[i],"-v")) verbose=1;
    }
    if(!infile||!outfile||site_lat==0){
        fprintf(stderr,"Usage: nids_site -i file.nids -o out.png "
                       "-la lat -lo lon [-p N0B|N0H] [-s size] [-v]\n");
        return 1;
    }

    NidsData nd;
    if(!parse_nids(infile,&nd)){
        fprintf(stderr,"ERROR: parse failed: %s\n",infile);
        return 1;
    }

    int is_n0h=(!strcmp(prod,"N0H"));

    /* Coverage bounds: site ± MAX_RANGE_KM */
    double km_per_lat=111.32;
    double km_per_lon=km_per_lat*cos(site_lat*DEG2RAD);
    double lat_range=MAX_RANGE_KM/km_per_lat;
    double lon_range=MAX_RANGE_KM/km_per_lon;
    double lat_min=site_lat-lat_range;
    double lat_max=site_lat+lat_range;
    double lon_min=site_lon-lon_range;
    double lon_max=site_lon+lon_range;

    /* Allocate canvas */
    uint8_t *rgba=calloc(px_size*px_size*4,1);
    if(!rgba) return 1;

    /* Render radials */
    for(int r=0;r<nd.n_radials;r++){
        float az0=nd.start_az[r];
        float daz=nd.delta_az[r];
        int nsub=(int)(daz/0.25f)+2;
        if(nsub<2) nsub=2;
        if(nsub>8) nsub=8;

        for(int b=0;b<nd.n_bins;b++){
            uint8_t val=nd.data[r][b];
            if(val<=1) continue;
            RGBA col=is_n0h?n0h_color(val):n0b_color(val);
            if(col.a==0) continue;

            double rng=(b+0.5)*BIN_SIZE_KM;
            if(rng>MAX_RANGE_KM) break;

            for(int sub=0;sub<nsub;sub++){
                float az_deg=az0+daz*sub/(float)(nsub-1);
                double az_rad=az_deg*DEG2RAD;
                double dx=rng*sin(az_rad);
                double dy=rng*cos(az_rad);
                double pt_lat=site_lat+dy/km_per_lat;
                double pt_lon=site_lon+dx/km_per_lon;

                if(pt_lat<lat_min||pt_lat>lat_max) continue;
                if(pt_lon<lon_min||pt_lon>lon_max) continue;

                int px=(int)((pt_lon-lon_min)/(lon_max-lon_min)*px_size);
                int py=(int)((lat_max-pt_lat)/(lat_max-lat_min)*px_size);
                if(px<0||px>=px_size||py<0||py>=px_size) continue;

                uint8_t *p=rgba+(py*px_size+px)*4;
                if(p[3]==0||val>p[3]){
                    p[0]=col.r;p[1]=col.g;p[2]=col.b;p[3]=col.a;
                }
            }
        }
    }

    /* Write PNG */
    FILE *f=fopen(outfile,"wb");
    if(!f){fprintf(stderr,"ERROR: cannot write %s\n",outfile);free(rgba);return 1;}
    png_structp png=png_create_write_struct(PNG_LIBPNG_VER_STRING,0,0,0);
    png_infop info=png_create_info_struct(png);
    if(setjmp(png_jmpbuf(png))){
        png_destroy_write_struct(&png,&info);fclose(f);free(rgba);return 1;
    }
    png_init_io(png,f);
    png_set_IHDR(png,info,px_size,px_size,8,PNG_COLOR_TYPE_RGBA,
                 PNG_INTERLACE_NONE,PNG_COMPRESSION_TYPE_DEFAULT,
                 PNG_FILTER_TYPE_DEFAULT);
    png_write_info(png,info);
    for(int y=0;y<px_size;y++)
        png_write_row(png,rgba+y*px_size*4);
    png_write_end(png,NULL);
    png_destroy_write_struct(&png,&info);
    fclose(f);
    free(rgba);

    /* Print bounds JSON to stdout for API to capture */
    printf("{\"lat_min\":%.4f,\"lat_max\":%.4f,"
           "\"lon_min\":%.4f,\"lon_max\":%.4f,"
           "\"site_lat\":%.4f,\"site_lon\":%.4f,"
           "\"n_radials\":%d,\"n_bins\":%d}\n",
           lat_min,lat_max,lon_min,lon_max,
           site_lat,site_lon,nd.n_radials,nd.n_bins);

    if(verbose)
        fprintf(stderr,"Rendered %s: %d radials × %d bins → %s\n",
                prod,nd.n_radials,nd.n_bins,outfile);
    return 0;
}
