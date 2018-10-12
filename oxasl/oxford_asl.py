#!/bin/env python
"""
OXASL: Converts ASL images into perfusion maps
==============================================

Michael Chappell, FMRIB Image Analysis Group & IBME

Copyright (c) 2008-2013 Univerisity of Oxford

Overview
--------

This module (and the associated command line tool) provide a 'one stop shop'
for analysing ASL data. They call the various other modules to preprocess,
correct, register, model and calibrate the data, outputting the perfusion
data, and also other estimated parameters.

The processing sequence is approximately:

1. Preprocess ASL data. This involves generation of mean and subtracted images
   and brain extracted versions. Not all of these images will be used later, but
   some might be.

2. Preprocess calibration data. This involves internal motion correction (for
   multi-volume calibration images), and the generation of mean and brain extracted
   versions of the calibration data.

3. Preprocess structural data. This involves generating brain extracted version of
   the structural data, and extracting any existing structural->standard space transformations
   (e.g. if an FSL_ANAT directory is the source of structural information)

4. If requested, the motion correction transformations for the ASL data are determined. 
   This is always done so that the middle volume of the
   ASL data is unchanged, although this volume is not always used as the reference.
   If applicable, the calibration->ASL registration is also determined.

5. If requested, the fieldmap distortion correction is determined from the fieldmap
   images

6. All available corrections are applied simultaneously to minimise interpolation

7. If required, a mask is generated from the corrected data

8. Model fitting is carried out to determine native space perfusion and other parameter maps

9. The ASL->structural registration is re-done using the perfusion image as the ASL reference.

10. If applicable, calibration is applied to the raw perfusion images

11. Raw and calibrated output images are generated in all output spaces (native, structural standard)
    
"""
from __future__ import print_function

import sys
import os
import traceback

import numpy as np

from fsl.data.image import Image

from oxasl import Workspace, __version__, image, calib, struc, basil, mask, corrections, reg
from oxasl.options import AslOptionParser, GenericOptions, OptionCategory, IgnorableOptionGroup

class OxfordAslOptions(OptionCategory):
    """
    OptionCategory which contains options for preprocessing ASL data
    """

    def __init__(self, **kwargs):
        OptionCategory.__init__(self, "oxasl", **kwargs)

    def groups(self, parser):
        ret = []
        g = IgnorableOptionGroup(parser, "Main Options")
        g.add_option("--wp", dest="wp", help="Analysis which conforms to the 'white papers' (Alsop et al 2014)", action="store_true", default=False)
        g.add_option("--mc", dest="mc", help="Motion correct data", action="store_true", default=False)
        g.add_option("--fixbat", dest="inferbat", help="Fix bolus arrival time", action="store_false", default=True)
        g.add_option("--fixbolus", dest="infertau", help="Fix bolus duration", action="store_false", default=True)
        g.add_option("--artoff", dest="inferart", help="Do not infer arterial component", action="store_false", default=True)
        g.add_option("--spatial-off", dest="spatial", help="Do not include adaptive spatial smoothing on CBF", action="store_false", default=True)
        ret.append(g)
        g = IgnorableOptionGroup(parser, "Acquisition/Data specific")
        g.add_option("--bat", help="Bolus arrival time (default=0.7 (pASL), 1.3 (cASL)", type=float)
        g.add_option("--batsd", help="Bolus arrival time standard deviation", type=float)
        g.add_option("--t1", help="Tissue T1 value", type=float, default=1.3)
        g.add_option("--t1b", help="Blood T1 value", type=float, default=1.65)
        ret.append(g)
        g = IgnorableOptionGroup(parser, "Output options")
        g.add_option("--save-corrected", help="Save corrected input data", action="store_true", default=False)
        g.add_option("--save-reg", help="Save registration information (transforms etc)", action="store_true", default=False)
        g.add_option("--save-basil", help="Save Basil modelling output", action="store_true", default=False)
        g.add_option("--save-calib", help="Save calibration output", action="store_true", default=False)
        g.add_option("--save-all", help="Save all output (enabled when --debug specified)", action="store_true", default=False)
        ret.append(g)
        return ret

def main():
    """
    Entry point for oxasl command line tool
    """
    debug = True
    try:
        parser = AslOptionParser(usage="oxasl -i <asl_image> [options]", version=__version__)
        parser.add_category(image.AslImageOptions())
        parser.add_category(struc.StructuralImageOptions())
        parser.add_category(OxfordAslOptions())
        parser.add_category(calib.CalibOptions(ignore=["perf", "tis"]))
        parser.add_category(corrections.DistcorrOptions())
        parser.add_category(GenericOptions())

        options, _ = parser.parse_args(sys.argv)
        if not options.output:
            options.output = "oxasl"

        # Some oxasl command-line specific defaults
        if options.calib is not None and options.calib_method is None:
            if options.struc is not None:
                options.calib_method = "refregion"
            else:
                options.calib_method = "voxelwise"
        if options.debug:
            options.save_all = True

        wsp = Workspace(savedir=options.output, separate_input=True, auto_asldata=True, **vars(options))
        #wsp.asldata = AslImage(options.asldata, **parser.filter(vars(options), "image"))

        oxasl(wsp)

    except Exception as e:
        sys.stderr.write("ERROR: " + str(e) + "\n")
        if debug:
            traceback.print_exc()
        sys.exit(1)

def oxasl(wsp):
    wsp.log.write("OXASL - running\n")
    wsp.log.write("Version: %s\n" % __version__)
    wsp.log.write("Input ASL data: %s\n" % wsp.asldata.name)
    wsp.asldata.summary(wsp.log)

    oxasl_preproc(wsp)
    oxasl_model(wsp)

    wsp.log.write("\nOutput is %s\n" % wsp.savedir)
    wsp.log.write("OXASL - done\n")

def oxasl_preproc(wsp):
    """
    Run standard processing on ASL data

    This method requires wsp to be a Workspace containing certain standard information.
    As a minimum, the attribute ``asldata`` must contain an AslImage object.
    """
    struc.init(wsp)
    corrections.apply_corrections(wsp)

    corrections.get_motion_correction(wsp)
    corrections.apply_corrections(wsp)

    reg.reg_asl2calib(wsp)
    reg.reg_asl2struc(wsp, True, False)

    corrections.get_fieldmap_correction(wsp)
    corrections.get_cblip_correction(wsp)
    corrections.get_sensitivity_correction(wsp)
    corrections.apply_corrections(wsp)

    mask.generate_mask(wsp)

def oxasl_model(wsp):
    if wsp.asldata.ntis > 1 and wsp.batsd is None:
        # For multi TI/PLD data, set a more liberal prior for tissue ATT since we should be able to 
        # determine this from the data. NB this leaves the arterial BAT alone.
        wsp.batsd = 1

    basil.basil(wsp, output_wsp=wsp.sub("basil"))

    # Re-do ASL->structural registration using BBR and perfusion image
    if wsp.structural.struc is not None:
        wsp.reg.regfrom_orig = wsp.reg.regfrom
        new_regfrom = np.copy(wsp.basil.finalstep.mean_ftiss.data)
        new_regfrom[new_regfrom < 0] = 0
        wsp.reg.regfrom = Image(new_regfrom, header=wsp.reg.regfrom.header)
        wsp.reg.asl2struc_initial = wsp.reg.asl2struc
        wsp.reg.struc2asl_initial = wsp.reg.struc2asl
        reg.reg_asl2struc(wsp, False, True)

    # FIXME: We could at this point re-apply corrections derived from structural space?
    # But would need to make sure corrections module re-transforms things like sensitivity map
    if wsp.pvcorr:
        # Partial volume correction is very sensitive to the mask, so recreate it
        # if it came from the structural image
        if wsp.mask is None and wsp.struc is not None:
            wsp.rois.mask_orig = wsp.rois.mask
            wsp.rois.mask = None
            mask.generate_mask(wsp)

        # Generate PVM and PWM maps for Basil
        struc.segment(wsp)
        wsp.structural.wm_pv_asl = reg.struc2asl(wsp, wsp.structural.wm_pv, use_applywarp=True)
        wsp.structural.gm_pv_asl = reg.struc2asl(wsp, wsp.structural.gm_pv, use_applywarp=True)
        wsp.basil_options = {"pwm" : wsp.structural.wm_pv_asl, "pgm" : wsp.structural.gm_pv_asl}
        basil.basil(wsp, output_wsp=wsp.sub("basil_pvcorr"), prefit=False)

    do_output(wsp)
    do_report(wsp)
    do_cleanup(wsp)

def do_report(wsp):
    """
    Generate HTML report
    """
    if not wsp.savedir:
        return
    report_build_dir = None
    if wsp.debug:
        report_build_dir = os.path.join(wsp.savedir, "report_build")
    wsp.log.write("\nGenerating HTML report\n")
    report_dir = os.path.join(wsp.savedir, "report")
    wsp.report.generate_html(report_dir, report_build_dir)
    wsp.log.write(" - Report generated in %s\n" % report_dir)

def do_output(wsp):
    """
    Create output images
    """
    calib.init(wsp)
    # Always output in native space
    output_spaces = ["native", ]
    output_items = {
        "mean_ftiss" : ("perfusion", 6000, True),
        "mean_fblood" : ("aCBV", 100, True),
        "mean_delttiss" : ("arrival", 1, False),
        "mean_ftisswm" : ("perfusion_wm", 6000, True),
        "mean_deltwm" : ("arrival_wm", 1, False),
        "modelfit" : ("modelfit", 1, False),
    }
    for space in output_spaces:
        output_wsp = wsp.sub(space)
        for fabber_output, oxasl_output in output_items.items():
            img = wsp.basil.finalstep.ifnone(fabber_output, None)
            if img is not None:
                # Make negative values = 0 and ensure masked value zeroed
                data = np.copy(img.data)
                data[img.data < 0] = 0
                data[wsp.rois.mask.data == 0] = 0
                img = Image(data, header=img.header)
                name, multiplier, is_perfusion = oxasl_output
                if is_perfusion:
                    img, = corrections.apply_sensitivity_correction(wsp, img)
                setattr(output_wsp, name, img)
                if is_perfusion and wsp.calib is not None:
                    wsp.multiplier = multiplier
                    setattr(output_wsp, "%s_calib" % name, calib.calibrate(wsp, img))

def do_cleanup(wsp):
    """
    Remove items from the workspace that are not being output. The
    corresponding files will be deleted
    """
    if not (wsp.save_all or wsp.save_corrected):
        wsp.corrected = None
        wsp.distcorr = None
        wsp.moco = None
        wsp.topup = None
        wsp.fieldmap = None
    if not (wsp.save_all or wsp.save_reg):
        wsp.reg = None
    if not (wsp.save_all or wsp.save_basil):
        wsp.basil = None
    if not (wsp.save_all or wsp.save_calib):
        wsp.calibration = None
