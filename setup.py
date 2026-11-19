from setuptools import Extension, setup
from Cython.Build import cythonize


setup(
    name="filemaker-viewer-reader",
    ext_modules=cythonize(
        [Extension("filemaker_reader", ["filemaker_reader.py"])],
        compiler_directives={"language_level": 3, "infer_types": True},
        annotate=False,
    ),
)
