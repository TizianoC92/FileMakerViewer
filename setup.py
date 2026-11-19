from setuptools import Extension, setup
from Cython.Build import cythonize


setup(
    name="filemaker-viewer-reader",
    ext_modules=cythonize(
        [Extension("fmp12_reader", ["fmp12_reader.py"])],
        compiler_directives={"language_level": 3, "infer_types": True},
        annotate=False,
    ),
)
