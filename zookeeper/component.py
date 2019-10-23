from abc import ABC, abstractmethod
from inspect import getmro

from prompt_toolkit import print_formatted_text

from zookeeper.utils import (
    get_concrete_subclasses,
    prompt_for_component,
    promt_for_param_value,
)

try:  # pragma: no cover
    from colorama import Fore

    BLUE, YELLOW, RESET = Fore.BLUE, Fore.YELLOW, Fore.RESET
except ImportError:  # pragma: no cover
    BLUE = YELLOW = RESET = ""

# Indent for nesting the string representation
INDENT = " " * 4


def str_key_val(key, value, color=True, single_line=False):
    if isinstance(value, Component):
        if single_line:
            value = repr(value)
        else:
            value = f"\n{INDENT}".join(str(value).split("\n"))
    elif callable(value):
        value = "<callable>"
    elif type(value) == str:
        value = f'"{value}"'
    space = "" if single_line else " "
    return (
        f"{BLUE}{key}{RESET}{space}={space}{YELLOW}{value}{RESET}"
        if color
        else f"{key}{space}={space}{value}"
    )


class Component(ABC):
    """
    A generic, modular component class that performs a context-specific action
    in `__call__`.

    Components can have configurable parameters, which can be either generic
    Python objects or nested sub-components. These are declared with class-level
    Python type annotations, in the same way that elements of
    [dataclasses](https://docs.python.org/3/library/dataclasses.html) are
    declared. After instantiation, components are 'hydrated' with a
    configuration dictionary; this process automatically injects the correct
    parameters into the component and all subcomponents. Component parameters
    can have defaults set, either in the class definition or passed via
    `__init__`, but configuration values passed to `hydrate` will always take
    precedence over these values.

    If a nested sub-component child declares a parameter with the same name as a
    parameter in one of its ancestors, it will receive the same configured value
    as the parent does. Howevever, configuration is scoped: if the parameter on
    the child, or on a _closer anscestor_, is configured with a different value,
    then that value will override the one from the original parent.

    Hydration can be interactive. In this case, the method will prompt for
    missing parameters via the CLI.

    The following example illustrates the hydration mechanism with scoped
    configuration:

    ```
    class A(Component):
        x: int
        z: float

        def __call__(self):
            return str(self.x) + "_" + str(self.z)

    class B(Component):
        a: A
        y: str = "foo"

        def __call__(self):
            return self.y + " / " + self.a()

    class C(Component):
        b: B
        x: int
        z: float = 3.14

        def __call__(self):
            return str(self.x) + "_" + str(self.z) + " / " + self.b()


    c = C()
    c.hydrate({
        "x": 5,                     # (1)
        "b.x": 10,                  # (2)
        "b.a.x": 15,                # (3)

        "b.y": "foo",               # (4)

        "b.z": 2.71                 # (5)
    })
    print(c)

    >>  C(
            b = B(
                a = A(
                    x = 15,         # (3) overrides (2) overrides (1)
                    z = 2.71        # Inherits from parent: (5)
                ),
                y = "foo"           # (4) overrides the default
            ),
            x = 5,                  # Only (1) applies
            z = 3.14                # The default is taken
        )
    ```
    """

    # The name of the component.
    __component_name__ = None

    # The default `__annotations__` attribute does not include annotations of
    # super classes, if applicable.
    @property
    def __component_annotations__(self):
        # Collect all annotations which apply to the class. Annotations aren't
        # inherited, so we have to go through the MRO chain and collect them
        # from all super classes, in reverse order so that they are correctly
        # overriden.
        annotations = {}
        for base_class in reversed(getmro(self.__class__)):
            annotations.update(getattr(base_class, "__annotations__", dict()))
        annotations.update(getattr(self, "__annotations__", {}))
        return annotations

    def __init__(self, **kwargs):
        """
        `kwargs` may only contain argument names corresponding to component
        annotations. The passed values will be set on the instance.
        """

        for k, v in kwargs.items():
            if k in self.__component_annotations__:
                setattr(self, k, v)
            else:
                raise ValueError(
                    f"Argument '{k}' passed to `__init__` does not correspond to any annotation of {self.__class__.__name__}."
                )

    def __init_subclass__(cls: type, *args, **kwargs):
        # Prohibit overriding `__init__` in subclasses.
        if cls.__init__ != Component.__init__:
            raise ValueError(
                f"Overriding `__init__` in component {cls}. `Component.__init__` "
                "must not be overriden, as doing so breaks the built-in "
                "hydration mechanism. It should be unnecessary to override "
                "`__init__`: the default `Component.__init__` implementation "
                "accepts keyword-arguments matching defined class attributes "
                "and sets the corresponding attribute values on the instance."
            )

    def hydrate(self, conf, parent=None, name=None, interactive=False):
        """
        Configure the component instance with parameters from the `conf` dict.

        Configuration passed through `conf` takes precedence over and will
        overwrite any values already set on the instance - either class default
        or those passed via `__init__`.
        """

        self.__component_parent__ = parent
        self.__component_name__ = name or self.__class__.__name__

        # Divide the annotations into those which are and those which are not
        # nested components. We will process the non-component parameters first,
        # because nested components may depend on parameter (non-component)
        # values set in the parent.
        non_component_annotations = []
        component_annotations = []

        for k, v in self.__component_annotations__.items():
            # We can use `issubclass` only when we know that `v` is a class.
            if isinstance(v, type) and issubclass(v, Component):
                component_annotations.append((k, v))
            else:
                non_component_annotations.append((k, v))

        # Process non-component annotations
        for k, v in non_component_annotations:
            # The value from the `conf` dict takes priority.
            if k in conf:
                param_value = conf[k]
                setattr(self, k, param_value)
            # If there's no config value but the value is already set on the
            # instance, add that value to the config so that it can get picked
            # up in child components.
            elif hasattr(self, k):
                conf[k] = getattr(self, k)
            # If we are running interactively, prompt for the missing value. Add
            # it to the configuration so that it gets passed to any children.
            elif interactive:
                param_name = f"{self.__component_name__}.{k}"
                param_value = promt_for_param_value(param_name, v)
                setattr(self, k, param_value)
                conf[k] = param_value

        # Process nested component annotations
        for k, v in component_annotations:
            # The value from the `conf` dict takes priority.
            if k in conf:
                instance = conf["k"]
                # Check that the configuration instance has the correct type.
                if not issubclass(instance, v):
                    raise ValueError(
                        f"The configured value '{instance}' for annotated parameter '{self.__component_name__}.{k}' must be an instance of '{v.__name__}'."
                    )
                setattr(self, k, instance)

            # If there's no config value but the value is set on the object, add
            # that value to the config so that it can get picked up in child
            # components.
            elif hasattr(self, k):
                conf[k] = getattr(self, k)

            # If there's only one concrete subclass of `v`, instantiate an
            # instance of that class.
            elif len(get_concrete_subclasses(v)) == 1:
                component_cls = list(get_concrete_subclasses(v))[0]
                print_formatted_text(
                    f"{component_cls} is the only concrete component class that satisfies the type of the annotated parameter '{self.__component_name__}.{k}'. Using an instance of this class by default."
                )
                # This is safe because we ban overriding `__init__`.
                instance = component_cls()
                setattr(self, k, instance)
                conf[k] = instance

            # If we are running interactively and there's more than one concrete
            # subclass of `v`, prompt for the concrete subclass to instantiate.
            # Add the instance to the configuation so that is can get passed to
            # any children.
            elif interactive and len(get_concrete_subclasses(v)) > 1:
                component_cls = prompt_for_component(
                    f"{self.__component_name__}.{k}", v
                )
                # The is safe because we ban overriding `__init__`.
                instance = component_cls()
                setattr(self, k, instance)
                conf[k] = instance

            # Hydrate the sub-component. The configuration we use consists of
            # all non-scoped keys and any keys scoped to `k`, where the keys
            # scoped to `k` override the non-scoped keys.
            non_scoped_conf = {a: b for a, b in conf.items() if "." not in a}
            k_scoped_conf = {
                a[len(f"{k}.") :]: b for a, b in conf.items() if a.startswith(f"{k}.")
            }
            nested_conf = {**non_scoped_conf, **k_scoped_conf}
            getattr(self, k).hydrate(
                nested_conf,
                parent=self,
                name=f"{self.__component_name__}.{k}",
                interactive=interactive,
            )

        # Validate all parameters.
        self.validate_configuration()

    def validate_configuration(self):
        """
        Called automatically at the end of `hydrate`. Subclasses should override
        this method to provide fine-grained control over parameter validation.
        Invalid configuration should be flagged by raising an error with a
        descriptive error message.

        The default implementation verifies that no annotated parameters are
        missing, and that the configured parameter values match the annotated
        type (where possible).
        """

        for name, annotated_type in self.__component_annotations__.items():
            if not hasattr(self, name):
                raise ValueError(
                    f"No configuration value found for annotated parameter '{self.__component_name__}.{name}' of type '{annotated_type.__name__ if isinstance(annotated_type, type) else annotated_type}'."
                )
            param_value = getattr(self, name)
            if isinstance(annotated_type, type):
                if not isinstance(param_value, annotated_type):
                    raise TypeError(
                        f"The configuration value {param_value} found for annotated parameter '{name}' must be of type '{annotated_type}', but has type '{type(param_value)}'."
                    )

    @abstractmethod
    def __call__(self):
        """
        Performs the context-specific action of the component. Must be overriden
        by concrete subclasses.
        """

        raise NotImplementedError

    def __str__(self):
        params = f",\n{INDENT}".join(
            [str_key_val(k, getattr(self, k)) for k in self.__component_annotations__]
        )
        return f"{self.__class__.__name__}(\n{INDENT}{params}\n)"

    def __repr__(self):
        params = ", ".join(
            [
                str_key_val(k, getattr(self, k), color=False, single_line=True)
                for k in self.__component_annotations__
            ]
        )
        return f"{self.__class__.__name__}({params})"