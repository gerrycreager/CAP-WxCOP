"""
sounding_render.py — shared Skew-T log-P + hodograph rendering for the
Soundings feature. Used by both the observed (TTAA/TTBB) and HRRR forecast
paths, since scripts/ingest_sounding.py and scripts/hrrr_sounding_extract.py
both produce the same profile shape:

    [{pressure_mb, height_m, temp_c, dewpoint_c, wind_dir_deg, wind_speed_kt}, ...]
    sorted surface -> 100mb (decreasing pressure)

Layout: full 100-1000mb Skew-T stays the primary/default view (a
meteorologist needs the whole column for CAPE/CIN, lifted index,
instability layers, and shear -- whole-column phenomena a boundary-layer
view can't substitute for), plus two additive insets:
  - Hodograph (standard placement, upper right)
  - Low-level detail panel (surface-700mb) with LCL annotated, aimed at
    CAP's actual operating envelope (mostly <=6000ft, often <=1500ft AGL)
    and the two things a pilot with basic Skew-T literacy is usually
    scanning for: lowered ceilings (LCL/saturation near the surface) and
    low-level wind "challenges".
"""
import io
import logging

import numpy as np
import matplotlib
matplotlib.use('Agg')  # headless -- no display available on the server
import matplotlib.pyplot as plt
from metpy.plots import SkewT, Hodograph
from metpy.units import units
import metpy.calc as mpcalc

log = logging.getLogger('sounding_render')


def _profile_arrays(profile):
    """Pull (pressure, temp, dewpoint, wind_dir, wind_speed) arrays with
    units out of the shared profile shape, skipping levels missing the
    pressure or temperature needed to plot at all."""
    p, T, Td, wdir, wspd = [], [], [], [], []
    for lv in profile:
        if lv.get('pressure_mb') is None or lv.get('temp_c') is None:
            continue
        p.append(lv['pressure_mb'])
        T.append(lv['temp_c'])
        Td.append(lv['dewpoint_c'] if lv.get('dewpoint_c') is not None else np.nan)
        wdir.append(lv['wind_dir_deg'] if lv.get('wind_dir_deg') is not None else np.nan)
        wspd.append(lv['wind_speed_kt'] if lv.get('wind_speed_kt') is not None else np.nan)
    return (
        np.array(p) * units.hPa,
        np.array(T) * units.degC,
        np.array(Td) * units.degC,
        np.array(wdir) * units.degrees,
        np.array(wspd) * units.knots,
    )


def render_skewt(profile, title=''):
    """Render a full-column Skew-T + hodograph + low-level detail inset for
    one sounding profile. Returns PNG bytes. Raises ValueError if the
    profile doesn't have enough usable levels to plot."""
    p, T, Td, wdir, wspd = _profile_arrays(profile)
    if len(p) < 2:
        raise ValueError('Not enough levels with pressure+temperature to plot a sounding')

    u, v = mpcalc.wind_components(wspd, wdir)

    fig = plt.figure(figsize=(11, 9))

    # Main Skew-T -- primary/default view, full 100-1000mb column.
    skew = SkewT(fig, rotation=45, rect=(0.08, 0.08, 0.58, 0.88))
    skew.plot(p, T, 'r', linewidth=2, label='Temperature')
    skew.plot(p, Td, 'g', linewidth=2, label='Dewpoint')
    finite_wind = np.isfinite(u.magnitude) & np.isfinite(v.magnitude)
    if finite_wind.any():
        skew.plot_barbs(p[finite_wind], u[finite_wind], v[finite_wind])
    skew.ax.set_ylim(1000, 100)
    skew.ax.set_xlim(-40, 40)
    skew.plot_dry_adiabats(alpha=0.25)
    skew.plot_moist_adiabats(alpha=0.25)
    skew.plot_mixing_lines(alpha=0.25)
    skew.ax.set_xlabel('Temperature (°C)')
    skew.ax.set_ylabel('Pressure (hPa)')
    skew.ax.set_title(title or 'Sounding', fontsize=12, fontweight='bold')

    # Hodograph inset -- standard placement, upper right.
    if finite_wind.sum() >= 2:
        hod_ax = fig.add_axes((0.68, 0.62, 0.28, 0.28))
        max_spd = float(np.nanmax(wspd.magnitude)) if np.isfinite(wspd.magnitude).any() else 50
        h = Hodograph(hod_ax, component_range=max(40, max_spd * 1.2))
        h.add_grid(increment=20)
        h.plot(u[finite_wind], v[finite_wind], color='navy', linewidth=2)
        hod_ax.set_title('Hodograph (kt)', fontsize=10)

    # Low-level detail inset -- surface to 700mb, LCL annotated. Additive,
    # not a replacement for the full column above.
    ll_ax = fig.add_axes((0.68, 0.08, 0.28, 0.45))
    mask = p.magnitude >= 700
    if mask.sum() >= 2:
        ll_ax.plot(T.magnitude[mask], p.magnitude[mask], 'r-', linewidth=2)
        ll_ax.plot(Td.magnitude[mask], p.magnitude[mask], 'g-', linewidth=2)
        ll_ax.set_ylim(float(p.magnitude[mask].max()), 700)
        ll_ax.set_xlabel('Temp (°C)', fontsize=9)
        ll_ax.set_ylabel('Pressure (hPa)', fontsize=9)
        ll_ax.set_title('Low-Level Detail (Sfc–700mb)', fontsize=10)
        ll_ax.grid(alpha=0.3)
        ll_ax.tick_params(labelsize=8)

        try:
            sfc_idx = int(np.argmax(p.magnitude[mask]))
            sfc_p = p[mask][sfc_idx]
            sfc_T = T[mask][sfc_idx]
            sfc_Td = Td[mask][sfc_idx]
            if np.isfinite(sfc_Td.magnitude):
                lcl_p, _lcl_T = mpcalc.lcl(sfc_p, sfc_T, sfc_Td)
                ll_ax.axhline(float(lcl_p.magnitude), color='blue', linestyle='--', alpha=0.8)
                ll_ax.annotate(
                    f'LCL ≈ {float(lcl_p.magnitude):.0f} hPa',
                    xy=(0.03, float(lcl_p.magnitude)),
                    xycoords=('axes fraction', 'data'),
                    fontsize=8, color='blue', va='bottom',
                )
        except Exception as e:
            log.debug('LCL calculation failed: %s', e)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=160, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()
