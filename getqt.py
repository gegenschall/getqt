#!/usr/bin/env python
import argparse
import tempfile
import urllib.request
import os
import sys
import subprocess
import shutil

from wheezy.template.engine import Engine
from wheezy.template.ext.core import CoreExtension
from wheezy.template.loader import FileLoader

DUMPBIN = 'C:\\Program Files (x86)\\Microsoft Visual Studio 12.0\\VC\\bin\\dumpbin.exe'
QT_LATEST = '5.4.1'
QT_REPOSITORY_BASE = 'http://download.qt.io/official_releases/qt/'
DEBUG_SUFFIX = 'd'
IGNORE_FILES = ['Qt5Designer', 'Qt5QmlDevTools']
FIXED_DEPS = ['qt5core']

MERGE_PACKAGES = {
	'qt5core': ['qtmain', 'qt5bootstrap', 'qt5platformsupport'],
	'qt5multimedia': ['qt5multimediawidgets'],
	'qt5opengl': ['qt5openglextensions'],
	'qt5xml': ['qt5xmlpatterns'],
	'qt5quick': ['qt5quickwidgets', 'qt5quicktest', 'qt5multimediaquick_p', 'qt5quickparticles'],
	'qt5webkit': ['qt5webkitwidgets'],
}

def file_to_package(filename):
	base = os.path.splitext(filename)[0].lower()
	
	if base.endswith(DEBUG_SUFFIX):
		return base[:-1]
		
	return base
	
class Component(object):
	TYPE_BINARY = 'dll'
	TYPE_SYMBOL = 'pdb'
	TYPE_LIBRARY = 'lib'
	
	BUILD_DEBUG = 'debug'
	BUILD_RELEASE = 'release'
	
	ARCH_X64 = 'x64'
	ARCH_X86 = 'x86'
	
	def __init__(self, path, targetpath, *args, **kwargs):
		self._abspath = path
		self._targetpath = targetpath
		self._arch = None
	
	@property
	def id(self):	
		return self.normalized_name + "-" + self.arch
	
	@property
	def type(self):		
		if self.extension == '.dll':
			return Component.TYPE_BINARY
		elif self.extension == '.pdb':
			return Component.TYPE_SYMBOL
		elif self.extension == '.lib':
			return Component.TYPE_LIBRARY
		else:
			raise Exception('Unkown file type')
	
	@property
	def arch(self):
		if self._arch is not None:
			return self._arch
	
		check_name = self._abspath
		# If this is a symbol file we don't know its arch. Exchange pdb to dll and try.
		if self.type == Component.TYPE_SYMBOL:
			test1 = os.path.splitext(self._abspath)[0] + '.dll'
			test2 = os.path.splitext(self._abspath)[0] + '.lib'
			
			if os.path.exists(test1):
				check_name = test1
			
			elif os.path.exists(test2):
				check_name = test2
			
			else:
				raise Exception("Cant find binary for symbol")
			
		out = subprocess.check_output('"' + DUMPBIN + '" /HEADERS ' + check_name)
		out = out.decode('utf-8').split('\r\n')
		for line in out:
			line = line.strip()
			if (line.startswith('8664') or line.startswith('14C')) and 'machine' in line:
				line = line.split(' ')
				self._arch = line[-1][1:-1]
				return self._arch
		
	@property
	def build(self):
		if self.extension == '.pdb':
			return Component.BUILD_DEBUG
			
		filebase = os.path.splitext(self.filename)[0]
		if filebase.endswith(DEBUG_SUFFIX):
			return Component.BUILD_DEBUG
			
		return Component.BUILD_RELEASE
	
	@property
	def filename(self):
		return os.path.basename(self._abspath)
		
	@property
	def normalized_name(self):
		return file_to_package(self.filename)
		
	@property
	def extension(self):
		return os.path.splitext(self._abspath)[1]
	
	@property
	def relpath(self):
		return os.path.relpath(self._abspath, self._targetpath)
	
	def __repr__(self):
		return "Component(%s, type=%s, build=%s, arch=%s)" % (self.filename, self.type, self.build, self.arch)
		
		
class DLLComponent(Component):
	def __init__(self, *args, **kwargs):
		super(DLLComponent, self).__init__(*args, **kwargs)
		
		self._deps = None
		
	@property
	def dependencies(self):
		if self._deps is None:
			self._deps = set()
			out = subprocess.check_output('"' + DUMPBIN + '" /DEPENDENTS ' + self._abspath)
			for dep in self._dumpbin_to_deps(out):
				if dep.startswith('qt'):
					self._deps.add(file_to_package(dep))
			
		return self._deps
			
	def _dumpbin_to_deps(self, instr):
		deps = []
		instr = instr.decode('utf-8')
		instr = instr.split('\r\n')
		deps_follow = False
		
		for line in instr:
			if line.strip() == '':
				continue
			if 'Summary' in line:
				deps_follow = False
			if deps_follow:
				deps.append(line.strip().lower())
			if 'dependencies' in line:
				deps_follow = True
			
		return deps

class PackageConfig(object):
	def __init__(self, arch, build, *args, **kwargs):
		self.arch = arch
		self.build = build
		
		self.binaries = []
		self.symbols = []
		self.libraries = []
		
	@property
	def all(self):
		return self.binaries + self.symbols + self.libraries
		
	def add_component(self, component):
		assert isinstance(component, Component)
		
		if component.type == Component.TYPE_BINARY:
			self.binaries.append(component)
		elif component.type == Component.TYPE_LIBRARY:
			self.libraries.append(component)
		elif component.type == Component.TYPE_SYMBOL:
			self.symbols.append(component)
		else:
			raise Exception("Unkown type")
		
class QtPackage(object):
	def __init__(self, id, name, *args, **kwargs):
		self.id = id.lower()
		self.name = name.lower()
		self.version = QT_LATEST

		self.configurations = set()
		self.dependencies = set()
		
	def get_config(self, arch, build):
		for config in self.configurations:
			if config.arch == arch and config.build == build:
				return config
				
		config = PackageConfig(arch, build)
		self.configurations.add(config)
		return config
		
	@classmethod
	def from_component(cls, component, vs_version='msvc2013'):
		assert isinstance(component, Component)
		# infer package name from dll name
		id = component.normalized_name + "-" + vs_version
		name = component.normalized_name
		
		package = cls(id, name)
		config = package.get_config(component.arch, component.build)
		config.add_component(component)
		return package
		
	def to_autopkg(self, outfile):		
		searchpath = [os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')]
		engine = Engine(
			loader=FileLoader(searchpath),
			extensions=[CoreExtension()]
		)
		template = engine.get_template('redist-packages.autopkg')
		autopkg = template.render({'package': self})
		
		with open(outfile, 'w') as f:
			f.write(autopkg)
		
		return autopkg
		
	def __repr__(self):
		return 'QtPackage(name=%s, id=%s, version=%s, configs=%d)' % (self.name, self.id, self.version, len(self.configurations))		

		
def compute_dependencies(packages):
	for packagename, package in packages.items():
		deps = set()
		deps.update(FIXED_DEPS)
		for config in package.configurations:
			for binary in config.binaries:			
				deps.update(binary.dependencies)
				if packagename in deps:
					deps.remove(packagename)
		
		package.dependencies = deps
		
def download_file(url, output):	
	file_name = url.split('/')[-1]
	target = os.path.join(output, file_name)
	u = urllib.request.urlopen(url)
	f = open(target, 'wb')
	file_size = int(u.getheader("Content-Length"))
	print("Downloading: %s Bytes: %s" % (file_name, file_size))

	file_size_dl = 0
	block_sz = 8192
	while True:
		buffer = u.read(block_sz)
		if not buffer:
			break

		file_size_dl += len(buffer)
		f.write(buffer)
		status = r"%10d  [%3.2f%%]" % (file_size_dl, file_size_dl * 100. / file_size)
		status = status + chr(8)*(len(status)+1)
		sys.stdout.write(status)

	f.close()

def get_qt_download_url(qt_version, vs_version, arch=''):
	if qt_version == 'latest':
		qt_version = QT_LATEST.split('.')
	
	version_path = '.'.join(qt_version[0:2]) + '/' + '.'.join(qt_version) + '/'
	qt_package = 'qt-opensource-windows-x86-%s%s_opengl-%s.exe' % (vs_version, arch, '.'.join(qt_version))
	download_url = QT_REPOSITORY_BASE + version_path + qt_package

	return download_url
	
def extract_qt_exe(path, file):
	print("Extracting " + file)
	fullfile = os.path.join(path, file)
	extractpath = "".join(fullfile.split('.')[:-1])
	with open(os.devnull, 'w') as tempf:
		subprocess.call('"' + fullfile + '" --dump-binary-data' + ' -o "' + extractpath + '"', stdout=tempf, stderr=tempf)
	
	return extractpath
	
def find_essentials_7z(inpath):
	# Recursively search for a file containing the essentials Qt distrib.
	# This is brutal but who cares.
	for dirpath, dirnames, filenames in os.walk(inpath):
		for filename in filenames:
			if 'essentials' in filename and filename.endswith('.7z'):
				return os.path.join(dirpath, filename)

def find_components(inpath, targetpath):
	components = []
	
	for dirpath, dirnames, filenames in os.walk(inpath):
		if dirpath.endswith('bin') or dirpath.endswith('lib'):
			for filename in filenames:
				skip = False
				for exclude in IGNORE_FILES:
					if exclude in filename:
						skip = True
						break
				
				if skip:
					continue
			
				if filename.endswith('.dll'):
					c = DLLComponent(os.path.join(dirpath, filename), targetpath)
					components.append(c)
					#print(c)
				
				elif filename.endswith('.pdb') or filename.endswith('.lib'):
					c = Component(os.path.join(dirpath, filename), targetpath)
					components.append(c)
					#print(c)				
					
	return components
	
def merge_packages(packagelist, targetname, members):
	assert targetname in packagelist
	target = packagelist.get(targetname)
	
	for packagename in members:
		assert packagename in packagelist
		package = packagelist.get(packagename)
		
		for config in package.configurations:
			target_config = target.get_config(config.arch, config.build)
			for component in config.all:
				target_config.add_component(component)
		
		del packagelist[packagename]
				
def extract_7zip_archive(file):
	print("Extracting " + file)
	output = tempfile.mkdtemp()
	with open(os.devnull, 'w') as tempf:
		subprocess.call('contrib\\7z.exe x "' + file + '" -o"' + output + '"', stdout=tempf, stderr=tempf)
	
	return output
	
def write_nupkgs(path):
	os.chdir(path)
	
	for filename in os.listdir(path):
		if not filename.endswith('.autopkg'):
			continue
		
		filebase = os.path.splitext(filename)[0]
		logfilename = filebase + '.log'
		
		print("Writing package " + filebase)
		
		with open(logfilename, 'w') as log:		
			subprocess.call('powershell Write-NuGetPackage ' + filename , stdout=log, stderr=log)
			
def copy_packages(source, destination):	
	for filename in os.listdir(source):
		extension = os.path.splitext(filename)[1]
		if extension in ('.autopkg', '.log', '.nupkg'):
			fullfilename = os.path.join(source, filename)
			fulldestination = os.path.join(destination, filename)
			
			print("Copy %s to %s" % (filename, destination))
			shutil.copyfile(fullfilename, fulldestination)
	
def main(args):
	tempdir = tempfile.mkdtemp()
	outputpath = os.path.abspath(args.output)
	qtpaths = []
	packages = {}
	print ('Temporary working directory: ' + tempdir)	
	print ('Output directory: ' + outputpath)	
	
	url64 = get_qt_download_url(args.qt_version, args.vs_version, '_64')
	url32 = get_qt_download_url(args.qt_version, args.vs_version)
	
	download_file(url64, tempdir)
	download_file(url32, tempdir)
	
	for filename in os.listdir(tempdir):
		if filename.endswith('.exe'):
			extracted = extract_qt_exe(tempdir, filename)
			zipfile = find_essentials_7z(extracted)
			qtpath = extract_7zip_archive(zipfile)
			qtpaths.append(qtpath)

	for qtpath in qtpaths:
		for component in find_components(qtpath, tempdir):
			print("Adding " + str(component))
			if component.normalized_name in packages:
				config = packages[component.normalized_name].get_config(component.arch, component.build)
				config.add_component(component)
			else:
				package = QtPackage.from_component(component)
				if args.qt_version == 'latest':
					package.version = QT_LATEST
				else:
					package.version = args.qt_version
				packages[package.name] = package
	
	for target, members in MERGE_PACKAGES.items():
		merge_packages(packages, target, members)
	
	compute_dependencies(packages)
	
	print("\nPackage dependencies: ")
	for id, package in packages.items():
		print(package.name + ' <- ' + str(package.dependencies))
	
	print("")
	
	for _, package in packages.items():
		autopkgfile = os.path.join(tempdir, package.id + '.autopkg')
		
		print('Writing ' + autopkgfile)
		package.to_autopkg(autopkgfile)

	print("")
		
	write_nupkgs(tempdir)

	print("")

	copy_packages(tempdir, outputpath)
	
	
if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Download Qt and create NuGet packages')
	parser.add_argument('output', help='Where to output packages')
	parser.add_argument('-q', '--qt-version', action='store', default='latest', help='Specify Qt version')
	parser.add_argument('--opengl', action='store_true', help='Use the OpenGL version of Qt')
	parser.add_argument('-s', '--vs-version', action='store', default='msvc2013', help='Target Visual Studio version')
	args = parser.parse_args()
	main(args)