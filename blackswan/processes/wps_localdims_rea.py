import os
# from datetime import date
from datetime import datetime as dt
from scipy.stats.mstats import mquantiles
import time  # performance test
import subprocess
# from subprocess import CalledProcessError
import uuid
import psutil

from netCDF4 import Dataset

from numpy import squeeze, savetxt, loadtxt, column_stack

from pywps import Process
from pywps import LiteralInput, LiteralOutput
from pywps import ComplexInput, ComplexOutput
from pywps import Format, FORMATS
from pywps.app.Common import Metadata

from blackswan.datafetch import _PRESSUREDATA_
from blackswan.datafetch import reanalyses as rl
from blackswan.ocgis_module import call
from blackswan import analogs
from blackswan import visualisation
from blackswan import config
from blackswan.utils import rename_complexinputs
from blackswan.utils import get_variable, get_time
from blackswan.utils import get_files_size

from blackswan.weatherregimes import _TIMEREGIONS_, _MONTHS_

#from blackswan.calculation import localdims, localdims_par
from blackswan.localdims import localdims, localdims_par

from blackswan.log import init_process_logger

import logging
LOGGER = logging.getLogger("PYWPS")


class LocaldimsReaProcess(Process):
    def __init__(self):
        inputs = [

            LiteralInput("reanalyses", "Reanalyses Data",
                         abstract="Choose a reanalyses dataset for comparison",
                         default="NCEP_slp",
                         data_type='string',
                         min_occurs=1,
                         max_occurs=1,
                         allowed_values=_PRESSUREDATA_
                         ),

            # LiteralInput("timeres", "Reanalyses temporal resolution",
            #              abstract="Temporal resolution of the reanalyses (only for 20CRV2)",
            #              default="day",
            #              data_type='string',
            #              min_occurs=0,
            #              max_occurs=1,
            #              allowed_values=['day', '6h']
            #              ),

            LiteralInput('BBox', 'Bounding Box',
                         data_type='string',
                         abstract="Enter a bbox: min_lon, max_lon, min_lat, max_lat."
                            " min_lon=Western longitude,"
                            " max_lon=Eastern longitude,"
                            " min_lat=Southern or northern latitude,"
                            " max_lat=Northern or southern latitude."
                            " For example: -80,50,20,70",
                         min_occurs=0,
                         max_occurs=1,
                         default='-20,40,30,70',
                         ),
            LiteralInput("season", "Time region",
                         abstract="Select the months to define the time region (all == whole year will be analysed)",
                         default="DJF",
                         data_type='string',
                         min_occurs=1,
                         max_occurs=1,
                         allowed_values=['January','February','March','April','May','June','July','August','September','October','November','December'] + list(_TIMEREGIONS_.keys())
                         ),
            LiteralInput('dateSt', 'Start date of analysis period',
                         data_type='date',
                         abstract='First day of the period to be analysed',
                         default='1948-01-01',
                         min_occurs=0,
                         max_occurs=1,
                         ),

            LiteralInput('dateEn', 'End date of analysis period',
                         data_type='date',
                         abstract='Last day of the period to be analysed',
                         default='1950-12-31',
                         min_occurs=0,
                         max_occurs=1,
                         ),

            LiteralInput("dist", "Distance",
                         abstract="Distance function to define analogues",
                         default='euclidean',
                         data_type='string',
                         min_occurs=0,
                         max_occurs=1,
                         allowed_values=['euclidean', 'mahalanobis', 'cosine']
                         ),

            LiteralInput("method", "Method",
                         abstract="Method of calculation: Python(full dist matrix at once), R(full dist matrix at once), R_wrap(dist matrix row by row on multiCPUs)",
                         default='Python_wrap',
                         data_type='string',
                         min_occurs=0,
                         max_occurs=1,
                         allowed_values=['Python', 'Python_wrap', 'R', 'R_wrap']
                         ),
        ]

        outputs = [

            ComplexOutput("ldist", "Distances File",
                          abstract="multi-column text file",
                          supported_formats=[Format("text/plain")],
                          as_reference=True,
                          ),
            ComplexOutput("ldist_seas", "Distances File for selected season (selection from all results)",
                          abstract="multi-column text file",
                          supported_formats=[Format("text/plain")],
                          as_reference=True,
                          ),
            ComplexOutput("ldist_csv", "Distances File with locdims for visualization",
                          abstract="multi-column csv file with rounded (dim 3) values ",
                          supported_formats=[Format("text/plain")],
                          as_reference=True,
                          ),
            ComplexOutput("q_csv", "Quantiles",
                          abstract="multi-column csv file",
                          supported_formats=[Format("text/plain")],
                          as_reference=True,
                          ),
            ComplexOutput("ld_pdf", "Scatter plot dims/theta",
                          abstract="Scatter plot dims/theta",
                          supported_formats=[Format('image/pdf')],
                          as_reference=True,
                          ),
            ComplexOutput("ld_pie", "Distribution of weather regimes",
                          abstract="Pie-plot of weather regimes",
                          supported_formats=[Format('image/pdf')],
                          as_reference=True,
                          ),
            ComplexOutput("ld2_pdf", "Scatter plot dims/theta",
                          abstract="Scatter plot dims/theta",
                          supported_formats=[Format('image/pdf')],
                          as_reference=True,
                          ),
            ComplexOutput("ld2_seas_pdf", "Scatter plot dims/theta for season",
                          abstract="Scatter plot dims/theta for season",
                          supported_formats=[Format('image/pdf')],
                          as_reference=True,
                          ),
            ComplexOutput("ld2_html", "Scatter plot dims/theta as html page",
                          abstract="Interactive visualization of localdims",
                          supported_formats=[Format("text/html")],
                          as_reference=True,
                          ),
            ComplexOutput("ld2_seas_html", "Scatter plot dims/theta for season as html page",
                          abstract="Interactive visualization of localdims",
                          supported_formats=[Format("text/html")],
                          as_reference=True,
                          ),
            ComplexOutput("zoom_html", "Scatter plot dims/theta for season as html page",
                          abstract="Interactive visualization of localdims",
                          supported_formats=[Format("text/html")],
                          as_reference=True,
                          ),
            ComplexOutput("zoom_csv", "Distances File with locdims for visualization",
                          abstract="multi-column csv file",
                          supported_formats=[Format("text/plain")],
                          as_reference=True,
                          ),
            ComplexOutput('output_log', 'Logging information',
                          abstract="Collected logs during process run.",
                          as_reference=True,
                          supported_formats=[Format('text/plain')]
                          ),
        ]

        super(LocaldimsReaProcess, self).__init__(
            self._handler,
            identifier="localdims_rea",
            title="Calculation of local dimensions and persistence (based on reanalyses data)",
            abstract='Local dimensions and persistence',
            version="0.10",
            metadata=[
                Metadata('LSCE', 'http://www.lsce.ipsl.fr/en/index.php'),
                Metadata('Doc', 'http://flyingpigeon.readthedocs.io/en/latest/'),
            ],
            inputs=inputs,
            outputs=outputs,
            status_supported=True,
            store_supported=True,
        )

    def _handler(self, request, response):

        os.chdir(self.workdir)
        init_process_logger('log.txt')

        LOGGER.info('Start process')
        response.update_status('execution started at : {}'.format(dt.now()), 5)

        process_start_time = time.time()  # measure process execution time ...
        start_time = time.time()  # measure init ...

        ################################
        # reading in the input arguments
        ################################

        try:
            response.update_status('read input parameter : %s ' % dt.now(), 6)

            dateSt = request.inputs['dateSt'][0].data
            dateEn = request.inputs['dateEn'][0].data

            # timres = request.inputs['timeres'][0].data
            timres = 'day'
            season = request.inputs['season'][0].data
            bboxDef = '-20,40,30,70'  # in general format

            bbox = []
            bboxStr = request.inputs['BBox'][0].data
            LOGGER.debug('BBOX selected by user: %s ' % (bboxStr))
            bboxStr = bboxStr.split(',')

            # Checking for wrong cordinates and apply default if nesessary
            if (abs(float(bboxStr[0])) > 180 or
                    abs(float(bboxStr[1]) > 180) or
                    abs(float(bboxStr[2]) > 90) or
                    abs(float(bboxStr[3])) > 90):
                bboxStr = bboxDef  # request.inputs['BBox'].default  # .default doesn't work anymore!!!
                LOGGER.debug('BBOX is out of the range, using default instead: %s ' % (bboxStr))
                bboxStr = bboxStr.split(',')

            bbox.append(float(bboxStr[0]))
            bbox.append(float(bboxStr[2]))
            bbox.append(float(bboxStr[1]))
            bbox.append(float(bboxStr[3]))
            LOGGER.debug('BBOX for ocgis: %s ' % (bbox))
            LOGGER.debug('BBOX original: %s ' % (bboxStr))

            distance = request.inputs['dist'][0].data
            method = request.inputs['method'][0].data

            model_var = request.inputs['reanalyses'][0].data
            model, var = model_var.split('_')

            LOGGER.info('input parameters set')
            response.update_status('Read in and convert the arguments', 7)
        except Exception as e:
            msg = 'failed to read input prameter %s ' % e
            LOGGER.exception(msg)
            raise Exception(msg)

        ######################################
        # convert types and set environment
        ######################################

        start = dateSt
        end = dateEn

        ###########################
        # set the environment
        ###########################

        response.update_status('fetching data from archive', 9)

        try:
            if model == 'NCEP':
                getlevel = False
                if 'z' in var:
                    level = var.strip('z')
                    # conform_units_to = None
                else:
                    level = None
                    if var == 'precip':
                        var = 'pr_wtr'
                    # conform_units_to = 'hPa'
            elif '20CRV2' in model:
                getlevel = False
                if 'z' in var:
                    level = var.strip('z')
                    # conform_units_to = None
                else:
                    level = None
                    # conform_units_to = 'hPa'
            else:
                LOGGER.exception('Reanalyses dataset not known')
            LOGGER.info('environment set for model: %s' % model)
        except Exception:
            msg = 'failed to set environment'
            LOGGER.exception(msg)
            raise Exception(msg)

        ##########################################
        # fetch Data from original data archive
        ##########################################

        # NOTE: If ref is say 1950 - 1990, and sim is just 1 week in 2017:
        # - ALL the data will be downloaded, 1950 - 2017
        try:
            model_nc = rl(start=start.year,
                          end=end.year,
                          dataset=model,
                          variable=var, timres=timres, getlevel=getlevel)
            LOGGER.info('reanalyses data fetched')
        except Exception:
            msg = 'failed to get reanalyses data'
            LOGGER.exception(msg)
            raise Exception(msg)

        response.update_status('subsetting region of interest', 10)
        # from flyingpigeon.weatherregimes import get_level
        LOGGER.debug("start and end time: %s - %s" % (start, end))
        time_range = [start, end]

        # Checking memory and dataset size
        model_size = get_files_size(model_nc)
        memory_avail = psutil.virtual_memory().available
        thrs = 0.3  # 50%
        if (model_size >= thrs * memory_avail):
            ser_r = True
        else:
            ser_r = False

        # ################################

        # For 20CRV2 geopotential height, daily dataset for 100 years is about 50 Gb
        # So it makes sense, to operate it step-by-step
        # TODO: need to create dictionary for such datasets (for models as well)
        # TODO: benchmark the method bellow for NCEP z500 for 60 years

#        if ('20CRV2' in model) and ('z' in var):
        if ('z' in var):
            tmp_total = []
            origvar = get_variable(model_nc)

            for z in model_nc:
                # tmp_n = 'tmp_%s' % (uuid.uuid1())
                b0 = call(resource=z, variable=origvar, level_range=[int(level), int(level)], geom=bbox,
                spatial_wrapping='wrap', prefix='levdom_' + os.path.basename(z)[0:-3])
                tmp_total.append(b0)

            tmp_total = sorted(tmp_total, key=lambda i: os.path.splitext(os.path.basename(i))[0])
            inter_subset_tmp = call(resource=tmp_total, variable=origvar, time_range=time_range)

            # Clean
            for i in tmp_total:
                tbr = 'rm -f %s' % (i)
                os.system(tbr)

            # Create new variable
            ds = Dataset(inter_subset_tmp, mode='a')
            z_var = ds.variables.pop(origvar)
            dims = z_var.dimensions
            new_var = ds.createVariable('z%s' % level, z_var.dtype, dimensions=(dims[0], dims[2], dims[3]))
            new_var[:, :, :] = squeeze(z_var[:, 0, :, :])
            # new_var.setncatts({k: z_var.getncattr(k) for k in z_var.ncattrs()})
            ds.close()
            model_subset_tmp = call(inter_subset_tmp, variable='z%s' % level)
        else:
            if ser_r:
                LOGGER.debug('Process reanalysis step-by-step')
                tmp_total = []
                for z in model_nc:
                    # tmp_n = 'tmp_%s' % (uuid.uuid1())
                    b0 = call(resource=z, variable=var, geom=bbox, spatial_wrapping='wrap',
                            prefix='Rdom_' + os.path.basename(z)[0:-3])
                    tmp_total.append(b0)
                tmp_total = sorted(tmp_total, key=lambda i: os.path.splitext(os.path.basename(i))[0])
                model_subset_tmp = call(resource=tmp_total, variable=var, time_range=time_range)
            else:
                LOGGER.debug('Using whole dataset at once')
                model_subset_tmp = call(resource=model_nc, variable=var,
                                        geom=bbox, spatial_wrapping='wrap', time_range=time_range,
                                        )

        # If dataset is 20CRV2 the 6 hourly file should be converted to daily.
        # Option to use previously 6h data from cache (if any) and not download daily files.

        if '20CRV2' in model:
            if timres == '6h':
                from cdo import Cdo

                cdo = Cdo(env=os.environ)
                model_subset = '%s.nc' % uuid.uuid1()
                tmp_f = '%s.nc' % uuid.uuid1()

                cdo_op = getattr(cdo, 'daymean')
                cdo_op(input=model_subset_tmp, output=tmp_f)
                sti = '00:00:00'
                cdo_op = getattr(cdo, 'settime')
                cdo_op(sti, input=tmp_f, output=model_subset)
                LOGGER.debug('File Converted from: %s to daily' % (timres))
            else:
                model_subset = model_subset_tmp
        else:
            model_subset = model_subset_tmp

        LOGGER.info('Dataset subset done: %s ', model_subset)

        response.update_status('dataset subsetted', 15)

        # ======================================

        LOGGER.debug("get_input_subset_dataset took %s seconds.",
                     time.time() - start_time)
        response.update_status('**** Input data fetched', 20)

        ########################
        # input data preperation
        ########################
        response.update_status('Start preparing input data', 30)
        start_time = time.time()  # measure data preperation ...

        # -----------------------
        #  try:
        #     import ctypes
        #     # TODO: This lib is for linux
        #     mkl_rt = ctypes.CDLL('libmkl_rt.so')
        #     nth = mkl_rt.mkl_get_max_threads()
        #     LOGGER.debug('Current number of threads: %s' % (nth))
        #     mkl_rt.mkl_set_num_threads(ctypes.byref(ctypes.c_int(64)))
        #     nth = mkl_rt.mkl_get_max_threads()
        #     LOGGER.debug('NEW number of threads: %s' % (nth))
        #     # TODO: Does it \/\/\/ work with default shell=False in subprocess... (?)
        #     os.environ['MKL_NUM_THREADS'] = str(nth)
        #     os.environ['OMP_NUM_THREADS'] = str(nth)
        # except Exception as e:
        #     msg = 'Failed to set THREADS %s ' % e
        #     LOGGER.debug(msg)
        # -----------------------

        response.update_status('Start DIM calc', 50)

        # Calculation of Local Dimentsions ==================
        LOGGER.debug('Calculation of the distances using: %s metric' % (distance))
        LOGGER.debug('Calculation of the dims with: %s' % (method))

        dim_filename = '%s.txt' % model
        tmp_dim_fn = '%s.txt' % uuid.uuid1()

        Rsrc = config.Rsrc_dir()

        if (method == 'Python'):
            try:
                l_dist, l_theta = localdims(resource=model_subset, variable=var, distance=str(distance))
                response.update_status('**** Dims with Python suceeded', 60)
            except:
                LOGGER.exception('NO! output returned from Python call')

        if (method == 'Python_wrap'):
            try:
                l_dist, l_theta = localdims_par(resource=model_subset, variable=var, distance=str(distance))
                response.update_status('**** Dims with Python suceeded', 60)
            except:
                LOGGER.exception('NO! output returned from Python call')

        if (method == 'R'):
            # from os.path import join
            Rfile = 'localdimension_persistence_fullD.R'
            args = ['Rscript', os.path.join(Rsrc, Rfile),
                    '%s' % model_subset, '%s' % var,
                    '%s' % tmp_dim_fn]
            LOGGER.info('Rcall builded')
            LOGGER.debug('ARGS: %s' % (args))

            try:
                output, error = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
                LOGGER.info('R outlog info:\n %s ' % output)
                LOGGER.exception('R outlog errors:\n %s ' % error)
                if len(output) > 0:
                    response.update_status('**** Dims with R suceeded', 60)
                else:
                    LOGGER.exception('NO! output returned from R call')
                # HERE READ DATA FROM TEXT FILES
                R_resdim = loadtxt(fname=tmp_dim_fn, delimiter=',')
                l_theta = R_resdim[:, 0]
                l_dist = R_resdim[:, 1]
            except:
                msg = 'Dim with R'
                LOGGER.exception(msg)
                raise Exception(msg)

        if (method == 'R_wrap'):
            # from os.path import join
            Rfile = 'localdimension_persistence_serrD.R'
            args = ['Rscript', os.path.join(Rsrc, Rfile),
                    '%s' % model_subset, '%s' % var,
                    '%s' % tmp_dim_fn]
            LOGGER.info('Rcall builded')
            LOGGER.debug('ARGS: %s' % (args))

            try:
                output, error = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
                LOGGER.info('R outlog info:\n %s ' % output)
                LOGGER.exception('R outlog errors:\n %s ' % error)
                if len(output) > 0:
                    response.update_status('**** Dims with R_wrap suceeded', 60)
                else:
                    LOGGER.exception('NO! output returned from R call')
                # HERE READ DATA FROM TEXT FILES
                R_resdim = loadtxt(fname=tmp_dim_fn, delimiter=',')
                l_theta = R_resdim[:, 0]
                l_dist = R_resdim[:, 1]
            except:
                msg = 'Dim with R_wrap'
                LOGGER.exception(msg)
                raise Exception(msg)

        try:
            res_times = get_time(model_subset)
        except:
            LOGGER.debug('Not standard calendar')
            res_times = analogs.get_time_nc(model_subset)

        # plot 1
        ld_pdf = analogs.pdf_from_ld(x=l_dist, y=l_theta)
        #

        res_times=[res_times[i].isoformat().strip().split("T")[0].replace('-','') for i in range(len(res_times))]

        # concatenation of values
        concat_vals = column_stack([res_times, l_theta, l_dist])
        savetxt(dim_filename, concat_vals, fmt='%s', delimiter=',')

        # output season
        try:
            seas = _TIMEREGIONS_[season]['month'] # [12, 1, 2]
            LOGGER.info('Season to grep from TIMEREGIONS: %s ' % season)
            LOGGER.info('Season N to grep from TIMEREGIONS: %s ' % seas)
        except:
            LOGGER.info('No months in TIMEREGIONS, moving to months')
            try:
                seas = _MONTHS_[season]['month'] # [1] or [2] or ...
                LOGGER.info('Season to grep from MONTHS: %s ' % season)
                LOGGER.info('Season N to grep from MONTHS: %s ' % seas)
            except:
                seas = [1,2,3,4,5,6,7,8,9,10,11,12]
        ind = []

        # TODO: change concat_vals[i][0][4:6] to dt_obj.month !!!
        for i in range(len(res_times)):
            if (int(concat_vals[i][0][4:6]) in seas[:]):
                ind.append(i)
        sf = column_stack([concat_vals[i] for i in ind]).T
        seas_dim_filename = season + '_' + dim_filename
        savetxt(seas_dim_filename, sf, fmt='%s', delimiter=',')

        # -------------------------- plot with R ---------------
        R_plot_file = 'plot_csv.R'
        ld2_pdf = '%s_local_dims.pdf' % model
        ld2_html = '%s_local_dims.html' % model
        ld2_seas_pdf = season + '_' + ld2_pdf
        ld2_seas_html = season + '_' + ld2_html

        args = ['Rscript', os.path.join(Rsrc, R_plot_file),
                '%s' % dim_filename,
                '%s' % ld2_pdf,
                '%s' % ld2_html]
        try:
            output, error = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
            LOGGER.info('R outlog info:\n %s ' % output)
            LOGGER.exception('R outlog errors:\n %s ' % error)
        except:
            msg = 'Could not produce plot'
            LOGGER.exception(msg)
            # TODO: Here need produce empty pdf(s) to pass to output

        # TODO check whether remove Rdat.pdf at this step
        args = ['Rscript', os.path.join(Rsrc, R_plot_file),
                '%s' % seas_dim_filename,
                '%s' % ld2_seas_pdf,
                '%s' % ld2_seas_html]
        try:
            output, error = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
            LOGGER.info('R outlog info:\n %s ' % output)
            LOGGER.exception('R outlog errors:\n %s ' % error)
        except:
            msg = 'Could not produce plot'
            LOGGER.exception(msg)
            # TODO: Here need produce empty pdf(s) to pass to output
        # 
        # ====================================================


        # ========= Prepare csv for d3.js visualizastion =====
        dim_csv_filename = '%s.csv' % model
        q_csv_filename = '%s_q.csv' % model
        loc_zoom_filename = 'loc_dim_zoom.csv'
        loc_zoom_html = os.path.join(config.data_path(), 'loc_dim_zoom.html')

        # TODO: Add quantiles as input parameters
        q1d = mquantiles(l_dist, 0.15, alphap=0.5, betap=0.5)
        q2d = mquantiles(l_dist, 0.85, alphap=0.5, betap=0.5)
        q1t = mquantiles(l_theta, 0.15, alphap=0.5, betap=0.5)
        q2t = mquantiles(l_theta, 0.85, alphap=0.5, betap=0.5)
        NAOp = [0]*len(res_times)
        NAOn = [0]*len(res_times)
        BLO = [0]*len(res_times)
        AR = [0]*len(res_times)
        MIX = [0]*len(res_times)

        for i in range(len(res_times)):
            if (l_dist[i] < q1d and l_theta[i] > q1t and l_theta[i] < q2t):
                NAOp[i]=1
            elif (l_dist[i] > q2d and l_theta[i] > q1t and l_theta[i] < q2t):
                BLO[i]=1
            elif (l_theta[i] > q2t and l_dist[i] > q1d and l_dist[i] < q2d):
                AR[i]=1
            elif (l_theta[i] < q1t and l_dist[i] > q1d and l_dist[i] < q2d):
                NAOn[i] = 1
            else:
                MIX[i] = 1

        l_theta_tr = [round(x,3) for x in l_theta]
        l_dist_tr = [round(x,3) for x in l_dist]

        # concat_csv_vals = column_stack([res_times, l_theta, l_dist, NAOp, NAOn, BLO, AR, MIX])
        concat_csv_vals = column_stack([res_times, l_theta_tr, l_dist_tr, NAOp, NAOn, BLO, AR, MIX])
        csv_head = 'Time,theta,dim,NAOp,NAOn,BLO,AR,MIX'
        savetxt(dim_csv_filename, concat_csv_vals, fmt='%s', delimiter=',', header = csv_head)

        q_csv_vals = column_stack([q1d,q2d,q1t,q2t])
        q_csv_head = 'q1d,q2d,q1t,q2t'
        savetxt(q_csv_filename, q_csv_vals, fmt='%s', delimiter=',', header = q_csv_head)
       
        zoom_vals = column_stack([res_times, l_theta_tr, l_dist_tr])
        zoom_head = 'Time,theta,dim'
        savetxt(loc_zoom_filename, zoom_vals, fmt='%s', delimiter=',', header = zoom_head, comments='')

        # Create pie-plot
        pie_pdf = visualisation.pdf_pie_ld(NAOp, NAOn, BLO, AR, MIX, output='wr_dist.pdf')

        # ====================================================

        response.update_status('preparing output', 80)
        response.outputs['ld_pie'].file = pie_pdf
        response.outputs['ldist_csv'].file = dim_csv_filename
        response.outputs['q_csv'].file = q_csv_filename
        response.outputs['ldist'].file = dim_filename
        response.outputs['ldist_seas'].file = seas_dim_filename
        response.outputs['ld_pdf'].file = ld_pdf

        response.outputs['ld2_pdf'].file = ld2_pdf
        response.outputs['ld2_seas_pdf'].file = ld2_seas_pdf
        response.outputs['ld2_html'].file = ld2_html
        response.outputs['ld2_seas_html'].file = ld2_seas_html

        response.outputs['zoom_csv'].file = loc_zoom_filename
        response.outputs['zoom_html'].file = loc_zoom_html

        response.update_status('execution ended', 100)
        LOGGER.debug("total execution took %s seconds.",
                     time.time() - process_start_time)
        response.outputs['output_log'].file = 'log.txt'
        return response
