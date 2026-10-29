# Bundled third-party components

## WholeWindowLiquidGlass

Source: [vientooscuro/WholeWindowLiquidGlass](https://github.com/vientooscuro/WholeWindowLiquidGlass).

The single Swift source was fetched on 22 October 2026. Git blob SHA: `dc1350f42946d517ee387f3e45e4225f106ce5d6`. License: MIT, © 2026 Daniil Nuzhdin. The original license is included in `ThirdParty/WholeWindowLiquidGlass-LICENSE.txt` and copied into the application bundle.

Local changes wrap the implementation in `PRISM_EXPERIMENTAL_GLASS`, suspend tuning for Reduce Transparency / Increase Contrast, and update the existing tuner configuration when the blur slider moves. The main backdrop uses uniform nonzero blur-band weights so radius changes produce visible blur; smaller control surfaces retain their existing treatment. Prism installs the backdrop behind a separate hosting view and shares the setup between its windows.

This component uses private Core Animation filter parameters. It is enabled for this local prototype, not an App Store implementation. A future macOS release may change those parameters or their behavior. Remove `PRISM_EXPERIMENTAL_GLASS` from Swift Active Compilation Conditions to exclude it and use the public native fallback. The renderer has a display-link loop, which should be measured before production use. It has no network or credential access.

## Raleway

Source: [Google Fonts — Raleway](https://github.com/google/fonts/tree/main/ofl/raleway).

Font Git blob SHA: `d81d42f537c7dc1bbde81b54e4c52a92977a8fde`. The unmodified variable font remains bundled as `Fonts/Raleway.ttf`, with its SIL Open Font License in `ThirdParty/Raleway-OFL.txt`. It was used by the previous wordmark and is not rendered in the current interface.

## Prata (retained asset)

Source: [Google Fonts — Prata](https://github.com/google/fonts/tree/main/ofl/prata), designed by Cyreal.

Font Git blob SHA: `6c0e862cdb2c2409546fb2c42a709319db43f538`. The unmodified `Prata-Regular.ttf` is bundled under `Fonts`, and the SIL Open Font License is bundled under `ThirdParty/Prata-OFL.txt`. This earlier wordmark asset remains bundled but is not used in the current interface. No font is installed system-wide or downloaded at runtime.
