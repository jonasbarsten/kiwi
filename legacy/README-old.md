# Setup

We use desktop mode so it is easier to fix stuff.
We install the module `MODEP` as we use this for vocoder.

The PD patch and sub patches lives in `/home/patch/Desktop/kiwi-pd/`.

## Autostart patch:

Create a directory in `/home/patch/.config` called `autostart`.
Then create a file `.desktop` in that directory with the following contents:

```
[Desktop Entry]
Type=Application
Name=kiwi
Exec=pd /home/patch/Desktop/kiwi-pd/kiwi.pd
StartupNotify=false
Terminal=false
```

## NB: no need t do this, just for reference:

On raspberry pi:

Change `/usr/local/patchbox-modules/puredata/patchbox-module.json`:

Replace `"amidiminder.service",` with:

```
{
    "service": "amidiminder.service",
    "config": "blah"
}
```

Now we could run `patchbox` and modules -> Pure Data. But we do not want that actually lol.

## Pianoteq plugin:

Put `Pianoteq 8.lv2` folder into `/home/patch/.vst/` and maaaaybe `Pianoteq 8.so` file into same folder.

## Pure data:

Install `[vstplugin~]` from Pure Data -> Find Externals
Sometimes the MIDI is not patched from the MIDI controller into PureData
I have manually patched it with `aconnectgui` or `aconnect xx:yy zz:ww` (found via `aconnect -i` or `acconect -o`) or with the application `Patchage` that comes with the patchbox os.

## MODEP:

We use the `MODEP` module for vocoder (talkbox). This boots automatically with the OS. There is a photo of the setup in my phone from around now (24-10-16) with the used effects. NB: you have to install the three effects in question via the MODEP web GUI.
To map it to midi controls, dere is a settings-wheel on each effect and then a small settings-icon on each parameter where you can etner what midi control should be used.

If this stops working it might be due to another module being selected in the terminal using `patchbox`. Then just run `patchbox` again -> Modules and `Modep`.
