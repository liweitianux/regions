# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import absolute_import, division, print_function
import string
import itertools
import re
import copy
from collections import OrderedDict
from astropy import units as u
from astropy import coordinates
from astropy import log
from astropy.utils.exceptions import AstropyUserWarning
from warnings import warn
from .core import Shape, ShapeList

__all__ = [
    'read_ds9',
    'DS9RegionParserWarning',
    'DS9RegionParserError',
    'DS9Parser',
    'DS9RegionParser',
    'CoordinateParser',
]


regex_global = re.compile("^#? *(-?)([a-zA-Z0-9]+)")
"""Regular expression to extract region type or coodinate system"""

regex_meta = re.compile("([a-zA-Z]+)(=)([^= ]+) ?")
"""Regular expression to extract meta attributes"""

regex_paren = re.compile("[()]")
"""Regular expression to strip parenthesis"""

regex_splitter = re.compile("[, ]")
"""Regular expression to split coordinate strings"""


class DS9RegionParserWarning(AstropyUserWarning):
    """
    A generic warning class for DS9 region parsing inherited from astropy's
    warnings
    """


class DS9RegionParserError(ValueError):
    """
    A generic error class for DS9 region parsing
    """


def read_ds9(filename, errors='strict'):
    """Read a ds9 region file in as a list of region objects.

    Parameters
    ----------
    filename : str
        The file path
    errors : ``warn``, ``ignore``, ``strict``
      The error handling scheme to use for handling parsing errors.
      The default is 'strict', which will raise a ``DS9RegionParserError``.
      ``warn`` will raise a warning, and ``ignore`` will do nothing
      (i.e., be silent).

    Returns
    -------
    regions : list
        Python list of `regions.Region` objects.
    """
    with open(filename) as fh:
        region_string = fh.read()

    parser = DS9Parser(region_string, errors=errors)
    parser.run()
    return parser.shapes.to_region()


class CoordinateParser(object):
    """Helper class to structure coordinate parser"""
    @staticmethod
    def parse_coordinate(string_rep, unit):
        """
        Parse a single coordinate
        """
        # Any ds9 coordinate representation (sexagesimal or degrees)
        if 'd' in string_rep or 'h' in string_rep:
            return coordinates.Angle(string_rep)
        elif unit is 'hour_or_deg':
            if ':' in string_rep:
                spl = tuple([float(x) for x in string_rep.split(":")])
                return coordinates.Angle(spl, u.hourangle)
            else:
                ang = float(string_rep)
                return coordinates.Angle(ang, u.deg)
        elif unit.is_equivalent(u.deg):
            # return coordinates.Angle(string_rep, unit=unit)
            if ':' in string_rep:
                ang = tuple([float(x) for x in string_rep.split(":")])
            else:
                ang = float(string_rep)
            return coordinates.Angle(ang, u.deg)
        else:
            return u.Quantity(float(string_rep), unit)

    @staticmethod
    def parse_angular_length_quantity(string_rep):
        """
        Given a string that is either a number or a number and a unit, return a
        Quantity of that string.  e.g.:

            23.9 -> 23.9*u.deg
            50" -> 50*u.arcsec
        """
        unit_mapping = {
            '"': u.arcsec,
            "'": u.arcmin,
            'r': u.rad,
            'i': u.dimensionless_unscaled,
        }
        has_unit = string_rep[-1] not in string.digits
        if has_unit:
            unit = unit_mapping[string_rep[-1]]
            return u.Quantity(float(string_rep[:-1]), unit=unit)
        else:
            return u.Quantity(float(string_rep), unit=u.deg)


class DS9Parser(object):
    """Parse a DS9 string

    This class transforms a DS9 string to a `~regions.io.ds9.ShapeList`. The
    result is stored as ``shapes`` attribute.

    Each line is tested for either containing a region type or a coordinate
    system. If a coordinate system is found the global coordsys state of the
    parser is modified. If a region type is found the
    `~regions.DS9RegionParser` is invokes to transform the line into a
    `~regions.Shape` object.

    Parameters
    ----------
    region_string : str
        DS9 region string
    errors : ``warn``, ``ignore``, ``strict``
      The error handling scheme to use for handling parsing errors.
      The default is 'strict', which will raise a ``DS9RegionParserError``.
      ``warn`` will raise a warning, and ``ignore`` will do nothing
      (i.e., be silent).
    """
    coordinate_systems = ['fk5', 'fk4', 'icrs', 'galactic', 'wcs', 'physical', 'image', 'ecliptic']
    coordinate_systems += ['wcs{0}'.format(letter) for letter in string.ascii_lowercase]
    """List of valid coordinate systems"""
    coordsys_mapping = dict(zip(coordinates.frame_transform_graph.get_names(),
                                coordinates.frame_transform_graph.get_names()))
    coordsys_mapping['ecliptic'] = 'geocentrictrueecliptic'
    """Map to convert coordinate system names"""

    def __init__(self, region_string, errors='strict'):
        if errors not in ('strict', 'ignore', 'warn'):
            msg = "``errors`` must be one of strict, ignore, or warn; is {}"
            raise ValueError(msg.format(errors))
        self.region_string = region_string
        self.errors = errors

        # Global states
        self.coordsys = None
        self.global_meta = {}

        # Results
        self.shapes = ShapeList()

    def __str__(self):
        ss = self.__class__.__name__
        ss += '\nErrors: {}'.format(self.errors)
        ss += '\nCoordsys: {}'.format(self.coordsys)
        ss += '\nGlobal meta: {}'.format(self.global_meta)
        ss += '\nShapes: {}'.format(self.shapes)
        ss += '\n'
        return ss

    def set_coordsys(self, coordsys):
        """Transform coordinate system

        # TODO: needs expert attention
        """
        if coordsys in self.coordsys_mapping:
            self.coordsys = self.coordsys_mapping[coordsys]
        else:
            self.coordsys = coordsys

    def run(self):
        """Run all steps"""
        for line_ in self.region_string.split('\n'):
            for line in line_.split(";"):
                self.parse_line(line)
                log.debug('Global state: {}'.format(self))

    def parse_line(self, line):
        """Parse one line"""
        log.debug('Parsing {}'.format(line))
        # Skip blanks
        if line == '':
            return

        # Skip comments
        if line[0] == '#':
            return

        # Special case / header: parse global parameters into metadata
        if line.lstrip()[:6] == 'global':
            self.global_meta = self.parse_meta(line)
            return

        # Try to parse the line
        region_type_search = regex_global.search(line)
        if region_type_search:
            include = region_type_search.groups()[0]
            region_type = region_type_search.groups()[1]
        else:
            self._raise_error("No region type found for line '{0}'.".format(line))
            return

        if region_type in self.coordinate_systems:
            # Found coord system definition
            self.set_coordsys(region_type)
            return
        if region_type not in DS9RegionParser.language_spec:
            self._raise_error("Region type '{0}' was identified, but it is not one of "
                              "the known region types.".format(region_type))
            return
        else:
            # Found region specification
            self.parse_region(include, region_type, line)

    def _raise_error(self, msg):
        if self.errors == 'warn':
            warn(msg, DS9RegionParserWarning)
        elif self.errors == 'strict':
            raise DS9RegionParserError(msg)

    @staticmethod
    def parse_meta(meta_str):
        """Parse the metadata for a single ds9 region string.

        Parameters
        ----------
        meta_str : str
            Meta string, the metadata is everything after the close-paren of the
            region coordinate specification. All metadata is specified as
            key=value pairs separated by whitespace, but sometimes the values
            can also be whitespace separated.

        Returns
        -------
        meta : `~collections.OrderedDict`
            Dictionary containing the meta data
        """
        regex_meta_split = [x for x in regex_meta.split(meta_str.strip()) if x]
        equals_inds = [i for i, x in enumerate(regex_meta_split) if x == '=']
        result = OrderedDict()
        for ii, jj in zip(equals_inds, equals_inds[1:] + [None]):
            key = regex_meta_split[ii - 1]
            val =  " ".join(regex_meta_split[ii + 1:jj - 1 if jj is not None else None])
            result[key] = val

        return result

    def parse_region(self, include, region_type, line):
        """Extract a Shape from a region string"""
        if self.coordsys is None:
            raise DS9RegionParserError("No coordinate system specified and a"
                                       " region has been found.")
        else:
            helper = DS9RegionParser(self.coordsys, region_type,
                                     self.global_meta, line)
            helper.parse()
            self.shapes.append(helper.shape)


# Global definitions to improve readability
radius = CoordinateParser.parse_angular_length_quantity
width = CoordinateParser.parse_angular_length_quantity
height = CoordinateParser.parse_angular_length_quantity
angle = CoordinateParser.parse_angular_length_quantity
coordinate = CoordinateParser.parse_coordinate


class DS9RegionParser(object):
    """Parse a DS9 region string

    This will turn a line containing a DS9 region into a Shape

    Parameters
    ----------
    coordsys : str
        Coordinate system
    include : str {'', '-'}
        Flag at the beginning of the line
    region_type : str
        Region type
    global_meta : dict
        Global meta data
    line : str
        Line to parse
    """
    coordinate_units = {'fk5': ('hour_or_deg', u.deg),
                        'fk4': ('hour_or_deg', u.deg),
                        'icrs': ('hour_or_deg', u.deg),
                        'geocentrictrueecliptic': (u.deg, u.deg),
                        'galactic': (u.deg, u.deg),
                        'physical': (u.dimensionless_unscaled, u.dimensionless_unscaled),
                        'image': (u.dimensionless_unscaled, u.dimensionless_unscaled),
                        'wcs': (u.dimensionless_unscaled, u.dimensionless_unscaled),
                        }
    """Coordinate unit transformations"""
    for letter in string.ascii_lowercase:
        coordinate_units['wcs{0}'.format(letter)] = (u.dimensionless_unscaled, u.dimensionless_unscaled)

    language_spec = {'point': (coordinate, coordinate),
                     'circle': (coordinate, coordinate, radius),
                     # This is a special case to deal with n elliptical annuli
                     'ellipse': itertools.chain((coordinate, coordinate),
                                                itertools.cycle((radius,))),
                     'box': (coordinate, coordinate, width, height, angle),
                     'polygon': itertools.cycle((coordinate,)),
                     'line': (coordinate, coordinate, coordinate, coordinate),
                     'annulus': itertools.chain((coordinate, coordinate),
                                                itertools.cycle((radius,))),
                    }
    """DS9 language specification. This defines how a certain region is read"""

    def __init__(self, coordsys, region_type, global_meta, line):

        self.coordsys = coordsys
        self.region_type = region_type
        self.global_meta = global_meta
        self.line = line

        self.meta_str = None
        self.coord_str = None
        self.include = None
        self.composite = None
        self.coord = None
        self.meta = None
        self.shape = None

    def __str__(self):
        ss = self.__class__.__name__
        ss += '\nLine : {}'.format(self.line)
        ss += '\nMeta string : {}'.format(self.meta_str)
        ss += '\nCoord string: {}'.format(self.coord_str)
        ss += '\nShape: {}'.format(self.shape)
        ss += '\n'
        return ss

    def parse(self):
        """Convert line to shape object"""
        log.debug(self)

        self.parse_composite()
        self.parse_include()
        self.split_line()
        self.convert_coordinates()
        self.convert_meta()
        self.make_shape()
        log.debug(self)

    def parse_composite(self):
        """Determine whether the region is composite"""
        self.composite = "||" in self.line

    def parse_include(self):
        """Determine wether to include/exclude the region"""
        if self.line[0] == '-':
            self.include = False
        else:
            self.include = True

    def split_line(self):
        """Split line into coordinates and meta string"""
        end_of_region_name = len(self.region_type)
        if not self.include:
            end_of_region_name += 1
        # coordinate of the # symbol or end of the line (-1) if not found
        hash_or_end = self.line.find("#")
        temp = self.line[end_of_region_name:hash_or_end].strip(" |")
        self.coord_str = regex_paren.sub("", temp)
        self.meta_str = self.line[hash_or_end:]

    def convert_coordinates(self):
        """Convert coordinate string to objects"""
        coord_list = []
        # strip out "null" elements, i.e. ''.  It might be possible to eliminate
        # these some other way, i.e. with regex directly, but I don't know how.
        # We need to copy in order not to burn up the iterators
        elements = [x for x in regex_splitter.split(self.coord_str) if x]
        element_parsers = self.language_spec[self.region_type]
        for ii, (element, element_parser) in enumerate(
            zip(elements, element_parsers)):
            if element_parser is coordinate:
                unit = self.coordinate_units[self.coordsys][ii % 2]
                coord_list.append(element_parser(element, unit))
            else:
                coord_list.append(element_parser(element))

        # Reset iterator for ellipse and annulus
        # Note that this cannot be done with copy.deepcopy on python2
        if self.region_type in ['ellipse', 'annulus']:
            self.language_spec[self.region_type] = itertools.chain(
                (coordinate, coordinate), itertools.cycle((radius,)))

        self.coord = coord_list

    def convert_meta(self):
        """Convert meta string to dict"""
        meta_ = DS9Parser.parse_meta(self.meta_str)
        self.meta = copy.deepcopy(self.global_meta)
        self.meta.update(meta_)

    def make_shape(self):
        """Make shape object"""
        self.shape = Shape(coordsys=self.coordsys,
                           region_type=self.region_type,
                           coord=self.coord,
                           meta=self.meta,
                           composite=self.composite,
                           include=self.include
                          )
