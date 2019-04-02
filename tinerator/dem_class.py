import richdem as rd
import numpy as np
from pylagrit import PyLaGriT
from copy import deepcopy
import logging

import tinerator.config as cfg
import tinerator.utilities as util
import tinerator.boundary as boundary
import tinerator.plot as tinplot
import tinerator.watershed_deliniation as delin
import tinerator.meshing as mesh
import tinerator.attributes as attrib

class DEM():
    '''
    This is the 'main' class of TINerator, and stores all DEM and GIS data
    related to a particular project.

    # Attributes
    filepath (str): Filepath to DEM raster
    lagrit_exe (str,None): Optional filepath to LaGriT binary. If PyLaGriT is
    configured correctly, this should be unnecessary.
    '''
    def __init__(self,filepath:str,lagrit_exe:str=None):
        self.dem = rd.LoadGDAL(filepath)
        self.lg = PyLaGriT(lagrit_exe=lagrit_exe,verbose=False)
        self.distance_field = None
        self.triangles = None
        self.feature = None
        self.boundary = None
        self.points = None
        self.stacked_mesh = None
        self.accumulation_matrix = None

        self.no_data_value = self.dem.no_data
        self.ncols = np.shape(self.dem)[1]
        self.nrows = np.shape(self.dem)[0]
        self.cell_size = self.dem.geotransform[1]
        self.xll_corner = self.dem.geotransform[0]
        self.yll_corner = self.dem.geotransform[3] - self.nrows * self.cell_size
        self.mask = self.dem == self.dem.no_data if self.no_data_value in self.dem else None

        self._surface_mesh = None

        # The below values are defaults and should be changed based on DEM
        self.max_distance = 20.
        self.min_distance = 0.3
        self.max_edge = 10.
        self.min_edge = 0.1

        # Mesh characteristics
        self.number_of_layers = 0

        # Temp files generated in the meshing process
        self.__dirty_files = []

        self.__replace_infs_with_nans()

    def __repr__(self):
        return 'DEM: %d rows, %d cols; cell size: %f' % \
        (self.nrows,self.ncols,self.cell_size)

    @property
    def extent(self):
        return self.getBoundingBox()

    @property
    def ratio(self):
        return self.nrows / self.ncols

    @property
    def min_z(self):
        return np.nanmin(self.dem)

    @property
    def max_z(self):
        return np.nanmax(self.dem)

    @property
    def top_layer(self):
        return 0

    @property
    def bottom_layer(self):
        return 0

    # Replace +/-float('inf') with np.nans which are more managable
    def __replace_infs_with_nans(self):
        for _invalid in [float('inf'),float('-inf')]:
            self.dem[self.dem == _invalid] = cfg.GLOBAL_NDV
            if self.no_data_value == _invalid:
                cfg.log.debug('Found %s ' % _invalid)
                self.change_ndv(GLOBAL_NDV)


    # Resets the DEM mask
    def __reset_mask(self):
        if self.no_data_value in self.dem:
            self.mask = self.dem == self.dem.no_data
        else:
            self.mask = None

    def change_ndv(self,ndv:float):
        '''
        Changes `no_data_value` of the DEM object.
        This should be used instead of resetting `dem.no_data_value`
        manually, as it makes deeper changes.

        # Example
        ```python
        dem.change_ndv(-9999.)
        print(dem.no_data_value) # -9999.0
        ```

        # Arguments
        ndv (float): New `no_data_value`
        '''

        self.no_data_value = ndv
        self.dem.no_data = ndv
        self.dem.wrap()
        self.__reset_mask()

    def set_to_ndv(self,value:float):
        '''
        Changes all occurances of `value` in the DEM data 
        to `no_data_value`.

        # Example
        ```
        dem.set_to_ndv(dem.dem[0][0])
        print(dem.dem == dem.no_data_value) # True
        ```

        # Arguments
        value (float): raster value to replace
        '''
        self.dem[self.dem == value] = self.no_data_value
        self.__reset_mask()

    def set_verbosity(self,verb_level:int,filename:str=None):
        '''
        Set the verbosity level of printed output.

        * `NOTHING` : Nothing (except warnings and related)
        * `INFO` : Log output
        * `FULL` : Log output and LaGriT output
        * `DEBUG` : Log output, LaGriT output, and turns on debug mode

        Each of these verbosity levels are variables in `tinerator.config`.

        # Example

        ```python
        dem.set_verbosity(tinerator.config.FULL)
        ```

        # Arguments
        verb_level (int): verbosity level 

        # Optional Arguments
        filename (str): file to write log output to
        '''

        cfg.DEBUG_MODE = False
        if verb_level == cfg.NOTHING:
            self.lg.verbose = False
            cfg.log.setLevel(logging.NOTSET)
        elif verb_level == cfg.DEBUG:
            self.lg.verbose = True
            cfg.log.setLevel(logging.DEBUG)
            cfg.DEBUG_MODE = True
        elif verb_level == cfg.INFO:
            self.lg.verbose = False
            cfg.log.setLevel(logging.INFO)
        elif verb_level == cfg.FULL:
            self.lg.verbose = False
            cfg.log.setLevel(logging.INFO)

        if filename is not None:
            cfg.log.warning('Dynamic file logging isn\'t enabled yet')

    def fill_depressions(self,
                         fill_depressions:bool=True,
                         fill_flats:bool=True):
        '''
        Fills flats and depressions on DEM. On meshes intended to be high-
        resolution, leaving flats and depressions untouched may cause solver
        issues. This method should be called before generating a triplane.

        # Arguments
        fill_depressions (bool): fill pits and depressions on DEM
        fill_flats (bool): fill flats on DEM

        # Example
        ```python
        dem1 = tin.load.from_file("example.asc")
        dem2 = tin.load.from_file("example.asc")

        dem1.fill_depressions()

        plt.imshow(dem1.dem - dem2.dem)
        plt.show()
        ```

        '''

        if fill_depressions:
            cfg.log.info('Filling depressions')
            rd.FillDepressions(self.dem,epsilon=False,in_place=True)

        if fill_flats:
            cfg.log.info('Filling flats')
            rd.ResolveFlats(self.dem,in_place=True)


    def watershed_delineation(self,
                              threshold:float,
                              method:str='D8'):
        '''
        Performs watershed delineation on a DEM and returns a set of points
        corresponding to the feature.

        Available methods are:

        * D8
        * D4
        * Rho8
        * Rho4
        * Dinf
        * Quinn
        * Holmgren
        * Freeman

        # Arguments
        threshold (float): threshold for determining feature from noise

        # Optional Arguments
        method (str): Flow calculation method
        
        # Returns
        Polyline of feature as ordered (x,y) pairs
        '''

        self.accumulation_matrix = delin.watershedDelineation(self.dem,method=method)

        if threshold is None:
            _thresh = np.unique(accumulation)
            threshold = _thresh[int(0.1*len(_thresh))]

        self.feature = delin.getFeatureTrace(self.accumulation_matrix,
                                             feature_threshold=threshold)

        if np.size(self.feature) == 0:
            raise ValueError("Feature trace is empty. " + \
                             "Try setting a lower threshold.")

        self.feature = util.xyVectorToProjection(self.feature,
                                                 self.cell_size,
                                                 self.xll_corner,
                                                 self.yll_corner,
                                                 self.nrows)


        return self.feature


    def _generate_boundary(self,distance:float,rectangular:bool=False):
        '''
        Generates a set of spaced nodes corresponding to the boundary of the DEM,
        where the boundary is defined as the intersection of no_data_values
        (nil) and elevation (non-nil) data.

        # Arguments
        distance (float): Euclidean distance between adjacent boundary nodes

        # Optional Arguments
        rectangular (bool): if True, generates a boundary around the extent of the DEM

        # Returns
        vertices of boundary
        '''

        if rectangular:
            self.boundary = boundary.rectangularBoundary(self.extent,distance)
            return self.boundary

        distance /= self.cell_size
        self.boundary = boundary.squareTraceBoundary(self.dem,
                                                     self.no_data_value,
                                                     dist=distance)

        self.boundary = util.xyVectorToProjection(self.boundary,
                                                  self.cell_size,
                                                  self.xll_corner,
                                                  self.yll_corner,
                                                  self.nrows)
        return self.boundary


    def build_uniform_triplane(self,
                               edge_length:float,
                               smooth_boundary:bool=False,
                               flip:str='y',
                               apply_elevation:bool=True,
                               outfile:str=None,
                               rectangular_boundary:bool=False):
        '''
        Generates a triplane with uniformly sized elements.

        # Attributes
        edge_length (float): desired lengths for triangle edges

        # Optional Arugments
        flip (str): flips array of the elevation raster along a given axis (x,y,xy)
        smooth_boundary (bool): If True, smooth the DEM boundary for better interpolation
        apply_elevation (bool): If True, interpolate DEM elevations onto surface mesh
        outfile (str): filepath to save generated mesh to
        rectangular_boundary (bool): set to true if the DEM domain is rectangular

        # Returns
        PyLaGriT mesh object
        '''

        self._generate_boundary(edge_length,rectangular=rectangular_boundary)
        self._surface_mesh = mesh._uniform_surface_mesh(self.lg,
                                                        self.boundary,
                                                        outfile=outfile,
                                                        min_edge=edge_length*1.75)

        if apply_elevation:
            mesh._intrp_elevation_to_surface(self.lg,
                                             self,
                                             self._surface_mesh,
                                             outfile=outfile,
                                             flip=flip,
                                             smooth_boundary=smooth_boundary)

        if outfile is not None:
            cfg.log.info('Saved mesh to \"%s\"' % outfile)

        return self._surface_mesh


    def build_refined_triplane(self,
                               min_edge_length:float,
                               max_edge_length:float,
                               outfile:str=None,
                               apply_elevation:bool=True,
                               slope:float=2.,
                               refine_dist:float=0.5,
                               flip:str='y',
                               smooth_boundary:bool=False,
                               rectangular_boundary:bool=False):
        '''
        Generates a refined triangular mesh, with a minimum refinement length 
        defined by h.

        # Attributes
        min_edge (float): triangle edge lengths

        # Optional Arugments
        plot (bool): display the triangulation on completion
        flip (str): flips array of the elevation raster along a given axis (x,y,xy)
        '''

        if self.feature is None:
            raise ValueError("Feature selection must be performed first")

        # Generate boundary at max edge length
        self._generate_boundary(max_edge_length,rectangular=rectangular_boundary)

        # Filter feature to min edge length
        feature = deepcopy(self.feature)
        feature = util.filter_points(feature,min_edge_length*1.75)

        self._surface_mesh = mesh._refined_surface_mesh(self.lg,
                                                        self.boundary,
                                                        feature,
                                                        min_edge_length*1.75,
                                                        refine_dist=refine_dist,
                                                        slope=slope,
                                                        delta=0.75,
                                                        outfile=outfile)

        if apply_elevation:
            mesh._intrp_elevation_to_surface(self.lg,
                                             self,
                                             self._surface_mesh,
                                             outfile=outfile,
                                             flip=flip,
                                             smooth_boundary=smooth_boundary)

        if outfile is not None:
            cfg.log.info('Saved mesh to \"%s\"' % outfile)

        return self._surface_mesh



    def build_layered_mesh(self,layers,matids=None,xy_subset=None,outfile=None):
        '''
        Builds a layered mesh from a triplane.

        # Arguments
        layers (list<float>): List of sequential layer thicknesses

        # Optional Arguments
        matids (list<int): List of numbers to set as material ID for a list
        outfile (str): Filepath to save mesh to
        xy_subset ():


        # Example
        ```python
        layers = [1.,1.,3.,10.,2.]
        matids = [1,1,2,1,3]

        dem.build_layered_mesh(layers,matids=matids)
        ```

        '''

        if self._surface_mesh is None:
            raise ValueError("A surface mesh is required - run triplane generation")

        self._stacked_mesh = mesh._stacked_mesh(self.lg,
                                                self._surface_mesh,
                                                outfile,
                                                layers,
                                                matids=matids,
                                                xy_subset=xy_subset)

        self.number_of_layers = len(layers)
        
        return self._stacked_mesh


    def add_attribute(self,data,layers=None,attribute_name=None,outfile=None,dtype=float):
        '''
        Adds an attribute to the stacked mesh, over one or more layers. Default is all.
        Data must be an NxM matrix - it does not necessarily have to be the same size at the DEM,
        but is recommended as it will be streched to span the domain of it.

        `attribute_name` will be the element-based attribute the data is written into.
        The default is 'material ID' (`itetclr`), but can be changed to any
        [a-z][A-Z][0-9] string (outside of reserved LaGriT keywords).

        # Arguments
        data (np.ndarray): NxM matrix of data to be written as matrix

        # Optional Arguments
        layers (list<int>): Layer IDs to write attributes to. Defaults to 'all'.
        attribute_name (str): Attribute name to store data in. Defaults to material ID
        outfile (str): Filename to write mesh to
        dtype (type): Data type of elements in data (`float` or `int`)
        '''

        # Check for the case that a user passes in a string
        # instead of a type
        if isinstance(dtype,str):
            if 'int' in dtype.lower():
                dtype = int
        
        self._stacked_mesh = attrib._add_attribute(self.lg,
                                                   data,
                                                   self._stacked_mesh,
                                                   [self.ncols,self.nrows],
                                                   self.number_of_layers,
                                                   self.extent,
                                                   attribute_name=attribute_name,
                                                   layers=layers,
                                                   outfile=outfile,
                                                   dtype=dtype)

        return self._stacked_mesh
        


    def remove_attribute(self,attribute_name,mesh=None):
        if mesh is None:
            mesh = self._stacked_mesh

        attrib.remove_attribute(mesh,attribute_name)


    def mapFunctionToAttribute(self,operator='+',layers=None,attribute_name=None,
                               outfile=None,fn=lambda layer: layer*100):
        '''

        Maps a function and on operator onto mesh data.
        The function fn should take one parameter: the current layer
        number. The operator will perform on the data and function result.

        In other words, the new attribute data will be a result of:

           attribute_data(layer) = attribute_data [operation] fn(layer)

        For fn = lambda layer: layer*100 and operator '+',

           attribute_data(layer) = attribute_data + layer*100

        meaning that if a selection of attribute data is

            [1,3,5,10,12...]

        then, with operator '+' and lambda layer: layer*100,

            layer 1: [101,103,105,110,112...]
            layer 2: [201,203,205,210,212...]
            layer 3: [301,103,305,310,312...]
            ... 

        '''

        outfile = self.stacked_mesh if outfile is None else outfile
        mapFunctionToAttribute(self.lg,self.stacked_mesh,
                               self.number_of_layers,layers=layers,
                               attribute_name=attribute_name,operator=operator,
                               fn=fn)

    def meshStatistics(self):
        return self.stacked_mesh.information()

    def generateFacesets(self,outfile,facesets=None,naive=False):
        '''
        Generate boundary face sets according to normal vectors and layer ID.

        :param lg: running instance of PyLaGriT
        :type lg: pylagrit.PyLaGriT
        :param outfile: filepath to save Exodus facesets
        :type outfile: str
        :param facesets: generated facesets integer array with length equal to boundary
        :type facesets: np.ndarray
        :param naive: flag to generate Exodus mesh with 3 facesets: top, bottom, sides
        :type naive: bool

        '''

        if self.stacked_mesh is None:
            raise ValueError("A stacked mesh must be generated before calling this function")

        if naive:
            mo = self.lg.read(self.stacked_mesh)
            generateFaceSetsNaive(self.lg,mo,outfile)
        else:
            assert facesets is not None, 'Function requires facesets array'
            generateComplexFacesets(self.lg,outfile,self.stacked_mesh,self.boundary,facesets)

    def getBoundingBox(self,mpl_style:bool=True):
        '''
        Returns the bounding box (or extent) of the DEM domain.

        By default, the format of the extent returned is:

            (x_min,x_max,y_min,y_max)

        By setting `mpl_style=False`, the format changes to:

            (x_min,y_min,x_max,y_max)

        Extent units are relative to the parent DEM coordinate system.

        # Optional Arguments
        mpl_style (bool): Change the format of the returned extent

        # Returns
        DEM domain bounding box
        '''

        if mpl_style:
            return (self.xll_corner,
                    self.ncols*self.cell_size+self.xll_corner,
                    self.yll_corner,
                    self.nrows*self.cell_size+self.yll_corner)
        else:
            return (self.xll_corner,
                    self.yll_corner,
                    self.ncols*self.cell_size+self.xll_corner,
                    self.nrows*self.cell_size+self.yll_corner)


    def plot_dem(self,hillshade:bool=False,plot_out:str=None):
        '''
        Plots the loaded DEM.
        '''
        tinplot.plot_dem(self,hillshade=hillshade,plot_out=plot_out)


    def plot_boundary(self):
        '''
        Plots the DEM domain boundary (if available).
        '''
        tinplot.plot_boundary(self)


    def plot_feature(self):
        '''
        Displays the feature captured by performing watershed delination
        (if available).
        '''
        tinplot.plot_feature(self)

