import json
from pathlib import Path
from typing import Dict, List, Callable, NamedTuple, Optional, Union
from urllib.parse import urljoin
import warnings

from django.apps import apps
from django.conf import settings
from django.core.checks import Warning

from django_vite.core.exceptions import (
    DjangoViteManifestError,
    DjangoViteAssetNotFoundError,
    DjangoViteConfigNotFoundError,
)
from django_vite.core.tag_generator import Tag, TagGenerator, attrs_to_str

DEFAULT_APP_NAME = "default"

def print_debug(msg):
    print(f"[django-vite] {msg}")

def vite_is_serving(config: "DjangoViteConfig") -> bool:
    """
    ====
    When running in "dev mode", check that an actual vite webserver is running
    If not: fallback to serving the actual bundled version read from the manifest.json
    ====
    When "dev mode" was false to begin with, this means that we're running in prod and should never
    check for a running vite webserver instance to begin with
    """
    if config.dev_mode:
        print_debug("Evaluating devmode...")
        # Hacky way to check if the vite webserver is serving something
        import requests
        from requests.adapters import HTTPAdapter
        import urllib3
        vite_webserver_url = f"http://{config.dev_server_host}:{config.dev_server_port}/"
        try:
            session = requests.Session()
            session.mount(vite_webserver_url, HTTPAdapter(max_retries=0))
            response = session.get(vite_webserver_url)
            return response.status_code == 404
        except urllib3.exceptions.MaxRetryError:
            return False
        except requests.exceptions.ConnectionError:
            return False
    else:
        return False

def open_manifest(path_to_manifest):
    try:
        return open(path_to_manifest, "r")
    except Exception as e:
        print_debug("Failed to open relative manifest, try fallback....")

    import io
    import requests
    response = requests.get(path_to_manifest)
    response.raise_for_status()
    manifest_file = io.StringIO(response.text)
    return manifest_file


class DjangoViteConfig(NamedTuple):
    """
    The arguments for a DjangoViteAppClient.
    """

    # If using in development or production mode.
    dev_mode: bool = False

    # Default Vite server protocol (http or https)
    dev_server_protocol: str = "http"

    # Default vite server hostname.
    dev_server_host: str = "localhost"

    # Default Vite server port.
    dev_server_port: int = 5173

    # Prefix for STATIC_URL.
    static_url_prefix: str = ""

    # Path to your manifest file generated by Vite.
    manifest_path: Optional[Union[Path, str]] = None

    # Motif in the "manifest.json" to find the polyfills generated by Vite.
    legacy_polyfills_motif: str = "legacy-polyfills"

    # Default Vite server path to HMR script.
    ws_client_url: str = "@vite/client"

    # Default Vite server path to React RefreshRuntime for @vitejs/plugin-react.
    react_refresh_url: str = "@react-refresh"


class ManifestEntry(NamedTuple):
    """
    Represent an entry for a file inside the "manifest.json".
    """

    file: str
    src: Optional[str] = None
    isEntry: Optional[bool] = False
    isDynamicEntry: Optional[bool] = False
    css: Optional[List[str]] = []
    imports: Optional[List[str]] = []
    dynamicImports: Optional[List[str]] = []


class ManifestClient:
    """
    A client for accessing entries in the compiled vite config's "manifest.json".
    Only parses manifest.json if dev_mode=False.

    Public Methods:
        get(path: str) -- return the ManifestEntry for the given path.
    """

    def __init__(
        self, config: DjangoViteConfig, app_name: str = DEFAULT_APP_NAME
    ) -> None:
        self._config = config
        self.app_name = app_name

        self.dev_mode = config.dev_mode if config.dev_mode and vite_is_serving(self._config) else False
        self.manifest_path = self._clean_manifest_path()
        self.legacy_polyfills_motif = config.legacy_polyfills_motif

        self._entries: Dict[str, ManifestEntry] = {}
        self.legacy_polyfills_entry: Optional[ManifestEntry] = None

        # Don't crash if there is an error while parsing manifest.json.
        # Running DjangoViteAssetLoader.instance().checks() on startup will log any
        # errors.
        if not self.dev_mode:
            try:
                self._entries, self.legacy_polyfills_entry = self._parse_manifest()
            except DjangoViteManifestError:
                pass

    def _clean_manifest_path(self) -> Path:
        """
        Get the manifest_path from the config.
        If it wasn't provided, set the default location to
        STATIC_ROOT / static_url_prefix / "manifest.json".

        Returns:
            Path -- the path to the vite config's manifest.json
        """
        initial_manifest_path = self._config.manifest_path
        if not initial_manifest_path:
            return (
                Path(settings.STATIC_ROOT)
                / self._config.static_url_prefix
                / "manifest.json"
            )
        elif not isinstance(initial_manifest_path, Path) and not initial_manifest_path.startswith("http"):
            return Path(initial_manifest_path)
        else:
            return initial_manifest_path

    def check(self) -> List[Warning]:
        """Check that manifest files are valid when dev_mode=False."""
        try:
            if not self.dev_mode:
                self._parse_manifest()
            return []
        except DjangoViteManifestError as exception:
            return [
                Warning(
                    exception,
                    id="django_vite.W001",
                    hint=(
                        f"Make sure you have generated a manifest file, "
                        f'and that DJANGO_VITE["{self.app_name}"]["manifest_path"] '
                        "points to the correct location."
                    ),
                )
            ]

    class ParsedManifestOutput(NamedTuple):
        # all entries within the manifest
        entries: Dict[str, ManifestEntry] = {}
        # The manifest entry for legacy polyfills, if it exists within the manifest
        legacy_polyfills_entry: Optional[ManifestEntry] = None

    def _parse_manifest(self) -> ParsedManifestOutput:
        """
        Read and parse the Vite manifest file.

        Returns:
            entries {Dict[str, ManifestEntry]} -- All entries within the manifest

            legacy_polyfills_entry {ManifestEntry} -- The manifest entry for legacy
                polyfills, if it exists within the manifest.json

        Raises:
            DjangoViteManifestError: if cannot load the file or JSON in file is
                malformed.
        """
        if self.dev_mode:
            return self.ParsedManifestOutput()

        entries: Dict[str, ManifestEntry] = {}
        legacy_polyfills_entry: Optional[ManifestEntry] = None

        try:
            with open_manifest(self.manifest_path) as manifest_file:
                manifest_content = manifest_file.read()
                manifest_json = json.loads(manifest_content)

                for path, manifest_entry_data in manifest_json.items():
                    filtered_manifest_entry_data = {
                        key: value
                        for key, value in manifest_entry_data.items()
                        if key in ManifestEntry._fields
                    }
                    manifest_entry = ManifestEntry(**filtered_manifest_entry_data)
                    entries[path] = manifest_entry
                    if self.legacy_polyfills_motif in path:
                        legacy_polyfills_entry = manifest_entry

                return self.ParsedManifestOutput(entries, legacy_polyfills_entry)

        except Exception as error:
            raise DjangoViteManifestError(
                f"Cannot read Vite manifest file for app {self.app_name} at "
                f"{self.manifest_path} : {str(error)}"
            ) from error

    def get(self, path: str) -> ManifestEntry:
        """
        Gets the manifest_entry for given path.

        Returns:
            ManifestEntry -- the ManifestEntry for your path

        Raises:
            DjangoViteAssetNotFoundError: if cannot find the file path in the manifest
                or if manifest was never parsed due to dev_mode=True.
        """
        if path not in self._entries:
            raise DjangoViteAssetNotFoundError(
                f"Cannot find {path} for app={self.app_name} in Vite manifest at "
                f"{self.manifest_path}"
            )

        return self._entries[path]


class DjangoViteAppClient:
    """
    An interface for generating assets and urls from one vite app.
    DjangoViteConfig provides the arguments for the client.
    """

    def __init__(
        self, config: DjangoViteConfig, app_name: str = DEFAULT_APP_NAME
    ) -> None:
        self._config = config
        self.app_name = app_name

        self.dev_server_protocol = config.dev_server_protocol
        self.dev_server_host = config.dev_server_host
        self.dev_server_port = config.dev_server_port
        self.static_url_prefix = config.static_url_prefix
        self.ws_client_url = config.ws_client_url
        self.react_refresh_url = config.react_refresh_url

        self.manifest = ManifestClient(config, app_name)

    def _get_dev_server_url(
        self,
        path: str,
    ) -> str:
        """
        Generates an URL to an asset served by the Vite development server.

        Keyword Arguments:
            path {str} -- Path to the asset.

        Returns:
            str -- Full URL to the asset.
        """
        static_url_base = urljoin(settings.STATIC_URL, self.static_url_prefix)
        if not static_url_base.endswith("/"):
            static_url_base += "/"

        ## Override to allow non-Django served local static files (like with Nginx)
        ##   this can be an actual url (localhost:port) and not a relative path
        static_url_base = urljoin('', self.static_url_prefix)
        if not static_url_base.endswith("/"):
            static_url_base += "/"

        return urljoin(
            f"{self.dev_server_protocol}://"
            f"{self.dev_server_host}:{self.dev_server_port}",
            urljoin(static_url_base, path),
        )

    def _get_production_server_url(self, path: str) -> str:
        """
        Generates an URL to an asset served during production.

        Keyword Arguments:
            path {str} -- Path to the asset.

        Returns:
            str -- Full URL to the asset.
        """

        production_server_url = path
        if prefix := self.static_url_prefix:
            if not prefix.endswith("/"):
                prefix += "/"
            production_server_url = urljoin(prefix, path)

        if apps.is_installed("django.contrib.staticfiles"):
            from django.contrib.staticfiles.storage import staticfiles_storage

            return staticfiles_storage.url(production_server_url)

        return production_server_url

    def generate_vite_asset(
        self,
        path: str,
        **kwargs: Dict[str, str],
    ) -> str:
        """
        Generates a <script> tag for this JS/TS asset, a <link> tag for
        all of its CSS dependencies, and a <link modulepreload>
        for the js dependencies, as listed in the manifest file
        (for production only).
        In development Vite loads all by itself.

        Arguments:
            path {str} -- Path to a Vite JS/TS asset to include.

        Returns:
            str -- All tags to import this file in your HTML page.

        Keyword Arguments:
            **kwargs {Dict[str, str]} -- Adds new attributes to generated
                script tags.

        Returns:
            str -- The <script> tag and all <link> tags to import
                this asset in your page.
        """

        if vite_is_serving(self._config):
            url = self._get_dev_server_url(path)
            return TagGenerator.script(
                url,
                attrs={"type": "module", **kwargs},
            )

        tags: List[Tag] = []
        manifest_entry = self.manifest.get(path)
        scripts_attrs = {"type": "module", "crossorigin": "", **kwargs}

        # Add dependent CSS
        tags.extend(self._load_css_files_of_asset(path))

        # Add the script by itself
        url = self._get_production_server_url(manifest_entry.file)
        tags.append(
            TagGenerator.script(
                url,
                attrs=scripts_attrs,
            )
        )

        # Preload imports
        preload_attrs = {
            "type": "text/javascript",
            "crossorigin": "anonymous",
            "rel": "modulepreload",
            "as": "script",
        }

        for dep in manifest_entry.imports:
            dep_manifest_entry = self.manifest.get(dep)
            dep_file = dep_manifest_entry.file
            url = self._get_production_server_url(dep_file)
            tags.append(
                TagGenerator.preload(
                    url,
                    attrs=preload_attrs,
                )
            )

        return "\n".join(tags)

    def preload_vite_asset(
        self,
        path: str,
    ) -> str:
        """
        Generates a <link modulepreload> tag for this JS/TS asset, a
        <link preload> tag for all of its CSS dependencies,
        and a <link modulepreload> for the js dependencies.
        In development this template tag renders nothing,
        since files aren't compiled yet.

        Arguments:
            path {str} -- Path to a Vite JS/TS asset to preload.

        Returns:
            str -- All tags to preload this file in your HTML page.

        Returns:
            str -- all <link> tags to preload
                this asset.
        """
        if vite_is_serving(self._config):
            return ""

        tags: List[Tag] = []
        manifest_entry = self.manifest.get(path)

        # Add the script by itself
        script_attrs = {
            "type": "text/javascript",
            "crossorigin": "anonymous",
            "rel": "modulepreload",
            "as": "script",
        }

        manifest_file = manifest_entry.file
        url = self._get_production_server_url(manifest_file)
        tags.append(
            TagGenerator.preload(
                url,
                attrs=script_attrs,
            )
        )

        # Add dependent CSS
        tags.extend(self._preload_css_files_of_asset(path))

        # Preload imports
        for dep in manifest_entry.imports:
            dep_manifest_entry = self.manifest.get(dep)
            dep_file = dep_manifest_entry.file
            url = self._get_production_server_url(dep_file)
            tags.append(
                TagGenerator.preload(
                    url,
                    attrs=script_attrs,
                )
            )

        return "\n".join(tags)

    def _preload_css_files_of_asset(
        self,
        path: str,
    ) -> List[Tag]:
        return self._generate_css_files_of_asset(
            path,
            tag_generator=TagGenerator.stylesheet_preload,
        ).tags

    def _load_css_files_of_asset(
        self,
        path: str,
    ) -> List[Tag]:
        return self._generate_css_files_of_asset(
            path,
            tag_generator=TagGenerator.stylesheet,
        ).tags

    class GeneratedCssFilesOutput(NamedTuple):
        # list of generated CSS tags
        tags: List[Tag]
        # list of already processed CSS tags
        already_processed: List[str]

    def _generate_css_files_of_asset(
        self,
        path: str,
        already_processed: Optional[List[str]] = None,
        tag_generator: Callable[[str], Tag] = TagGenerator.stylesheet,
    ) -> GeneratedCssFilesOutput:
        """
        Generates all CSS tags for dependencies of an asset.

        Arguments:
            path {str} -- Path to an asset in the 'manifest.json'.
            config_key {str} -- Key of the configuration to use.
            already_processed {list} -- List of already processed CSS file.

        Returns:
            tags -- List of CSS tags.
            already_processed -- List of already processed css paths
        """
        if already_processed is None:
            already_processed = []
        tags: List[Tag] = []
        manifest_entry = self.manifest.get(path)

        for import_path in manifest_entry.imports:
            new_tags, _ = self._generate_css_files_of_asset(
                import_path, already_processed, tag_generator
            )
            tags.extend(new_tags)

        for css_path in manifest_entry.css:
            if css_path not in already_processed:
                url = self._get_production_server_url(css_path)
                tags.append(tag_generator(url))
                already_processed.append(css_path)

        return self.GeneratedCssFilesOutput(tags, already_processed)

    def generate_vite_asset_url(self, path: str) -> str:
        """
        Generates only the URL of an asset managed by ViteJS.
        Warning, this function does not generate URLs for dependant assets.

        Arguments:
            path {str} -- Path to a Vite asset.

        Returns:
            str -- The URL of this asset.
        """

        if vite_is_serving(self._config):
            return self._get_dev_server_url(path)

        manifest_entry = self.manifest.get(path)

        return self._get_production_server_url(manifest_entry.file)

    def generate_vite_legacy_polyfills(
        self,
        **kwargs: Dict[str, str],
    ) -> str:
        """
        Generates a <script> tag to the polyfills
        generated by '@vitejs/plugin-legacy' if used.
        This tag must be included at end of the <body> before
        including other legacy scripts.

        Keyword Arguments:
            **kwargs {Dict[str, str]} -- Adds new attributes to generated
                script tags.

        Raises:
            DjangoViteAssetNotFoundError: If polyfills path not found inside
                the 'manifest.json'.

        Returns:
            str -- The script tag to the polyfills.
        """

        if vite_is_serving(self._config):
            return ""

        polyfills_manifest_entry = self.manifest.legacy_polyfills_entry

        if not polyfills_manifest_entry:
            raise DjangoViteAssetNotFoundError(
                f"Vite legacy polyfills not found in manifest "
                f"at {self.manifest.manifest_path}"
            )

        scripts_attrs = {"nomodule": "", "crossorigin": "", **kwargs}
        url = self._get_production_server_url(polyfills_manifest_entry.file)

        return TagGenerator.script(
            url,
            attrs=scripts_attrs,
        )

    def generate_vite_legacy_asset(
        self,
        path: str,
        **kwargs: Dict[str, str],
    ) -> str:
        """
        Generates a <script> tag for legacy assets JS/TS
        generated by '@vitejs/plugin-legacy'
        (in production only, in development do nothing).

        Arguments:
            path {str} -- Path to a Vite asset to include
                (must contains '-legacy' in its name).

        Keyword Arguments:
            **kwargs {Dict[str, str]} -- Adds new attributes to generated
                script tags.

        Raises:
            DjangoViteAssetNotFoundError: If cannot find the asset path in the
                manifest (only in production).

        Returns:
            str -- The script tag of this legacy asset .
        """

        if vite_is_serving(self._config):
            return ""

        manifest_entry = self.manifest.get(path)
        scripts_attrs = {"nomodule": "", "crossorigin": "", **kwargs}
        url = self._get_production_server_url(manifest_entry.file)

        return TagGenerator.script(
            url,
            attrs=scripts_attrs,
        )

    def generate_vite_ws_client(self, **kwargs: Dict[str, str]) -> str:
        """
        Generates the script tag for the Vite WS client for HMR.
        Only used in development, in production this method returns
        an empty string.

        Returns:
            str -- The script tag or an empty string.

        Keyword Arguments:
            **kwargs {Dict[str, str]} -- Adds new attributes to generated
                script tags.
        """

        if not vite_is_serving(self._config):
            return ""

        url = self._get_dev_server_url(self.ws_client_url)

        return TagGenerator.script(
            url,
            attrs={"type": "module", **kwargs},
        )

    def generate_vite_react_refresh_url(self, **kwargs: Dict[str, str]) -> str:
        """
        Generates the script for the Vite React Refresh for HMR.
        Only used in development, in production this method returns
        an empty string.

        Keyword Arguments:
            **kwargs {Dict[str, str]} -- Adds new attributes to generated
                script tags.

        Returns:
            str -- The script or an empty string.
            config_key {str} -- Key of the configuration to use.
        """

        if not vite_is_serving(self._config):
            return ""

        url = self._get_dev_server_url(self.react_refresh_url)
        attrs_str = attrs_to_str(kwargs)

        return f"""<script type="module" {attrs_str}>
            import RefreshRuntime from '{url}'
            RefreshRuntime.injectIntoGlobalHook(window)
            window.$RefreshReg$ = () => {{}}
            window.$RefreshSig$ = () => (type) => type
            window.__vite_plugin_react_preamble_installed__ = true
        </script>"""


class DjangoViteAssetLoader:
    """
    Class handling Vite asset loading.

    Routes asset and url generation to the proper DjangoViteAppClient.
    """

    _instance = None
    _apps: Dict[str, DjangoViteAppClient]

    DJANGO_VITE = "DJANGO_VITE"

    LEGACY_DJANGO_VITE_SETTINGS: Dict[str, Optional[str]] = {
        "DJANGO_VITE_DEV_MODE": "dev_mode",
        "DJANGO_VITE_DEV_SERVER_PROTOCOL": "dev_server_protocol",
        "DJANGO_VITE_DEV_SERVER_HOST": "dev_server_host",
        "DJANGO_VITE_DEV_SERVER_PORT": "dev_server_port",
        "DJANGO_VITE_STATIC_URL_PREFIX": "static_url_prefix",
        "DJANGO_VITE_MANIFEST_PATH": "manifest_path",
        "DJANGO_VITE_LEGACY_POLYFILLS_MOTIF": "legacy_polyfills_motif",
        "DJANGO_VITE_WS_CLIENT_URL": "ws_client_url",
        "DJANGO_VITE_REACT_REFRESH_URL": "react_refresh_url",
        "DJANGO_VITE_ASSETS_PATH": None,
    }

    def __init__(self) -> None:
        raise RuntimeError("Use the instance() method instead.")

    @classmethod
    def instance(cls):
        """
        Singleton.
        Uses singleton to keep parsed manifests in memory after
        the first time they are loaded.

        Returns:
            DjangoViteAssetLoader -- only instance of the class.
        """

        if cls._instance is None:
            cls._instance = cls.__new__(cls)
            cls._instance._apps = {}

            cls._apply_django_vite_settings()
            cls._apply_legacy_django_vite_settings()
            cls._apply_default_fallback()

        return cls._instance

    def check(self, **kwargs) -> List[Warning]:
        """Check that manifest files are valid for apps with dev_mode=False."""
        errors: List[Warning] = []
        for app_client in self._apps.values():
            manifest_warnings = app_client.manifest.check()
            errors.extend(manifest_warnings)
        return errors

    @classmethod
    def _apply_django_vite_settings(cls):
        """
        Takes DjangoViteConfigs from the DJANGO_VITE setting, and plugs them into
        DjangoViteAppClients.
        """

        django_vite_settings = getattr(settings, cls.DJANGO_VITE, None)

        if not django_vite_settings:
            return

        for app_name, config in django_vite_settings.items():
            if not isinstance(config, DjangoViteConfig):
                config = DjangoViteConfig(**config)
            cls._instance._apps[app_name] = DjangoViteAppClient(config, app_name)

    @classmethod
    def _apply_legacy_django_vite_settings(cls):
        """
        If the project hasn't yet migrated to the new way of configuring django-vite,
        then plug values from the legacy settings into an app named "default".
        """

        applied_settings = dir(settings)
        legacy_settings_keys = cls.LEGACY_DJANGO_VITE_SETTINGS.keys()
        applied_legacy_settings = [
            key for key in legacy_settings_keys if key in applied_settings
        ]

        if not applied_legacy_settings:
            return

        # If there are both new DJANGO_VITE settings as well as legacy settings, then
        # allow _apply_django_vite_settings to apply only the DJANGO_VITE configs and
        # ignore the legacy settings.
        if cls.DJANGO_VITE in applied_settings:
            warnings.warn(
                f"You're mixing the new {cls.DJANGO_VITE} setting with these "
                f"legacy settings: [{', '.join(applied_legacy_settings)}]. Those legacy "
                f"settings will be ignored since you have a {cls.DJANGO_VITE}"
                " setting configured. Please remove those legacy django-vite settings.",
                DeprecationWarning,
            )
            return

        # Apply legacy settings, with a warning.
        warnings.warn(
            f"The settings [{', '.join(applied_legacy_settings)}] will be removed "
            "in future releases of django-vite. Please switch to defining your "
            f'settings as {cls.DJANGO_VITE} = {{"default": {{...}},}}.',
            DeprecationWarning,
        )

        legacy_config = {}
        for legacy_setting in applied_legacy_settings:
            new_config_name = cls.LEGACY_DJANGO_VITE_SETTINGS[legacy_setting]
            if new_config_name:
                legacy_config[new_config_name] = getattr(settings, legacy_setting)
        legacy_config = DjangoViteConfig(**legacy_config)
        cls._instance._apps[DEFAULT_APP_NAME] = DjangoViteAppClient(legacy_config)

    @classmethod
    def _apply_default_fallback(cls):
        """
        If no settings at all were provided (and no DjangoViteAppClient were
        instantiated), we can create a "default" DjangoViteAppClient using the default
        values of DjangoViteConfig.
        """

        if not cls._instance._apps:
            default_config = DjangoViteConfig()
            cls._instance._apps[DEFAULT_APP_NAME] = DjangoViteAppClient(default_config)

    def _get_app_client(self, app: str) -> DjangoViteAppClient:
        """
        Gets the DjangoViteAppClient for given app.

        Returns:
            DjangoViteAppClient -- the client for your app

        Raises:
            DjangoViteConfigNotFoundError: If app was not found in DJANGO_VITE
                settings.
        """

        if app not in self._apps:
            raise DjangoViteConfigNotFoundError(
                f"Cannot find {app} in {self.DJANGO_VITE} settings."
            )

        return self._apps[app]

    def generate_vite_asset(
        self,
        path: str,
        app: str = DEFAULT_APP_NAME,
        **kwargs: Dict[str, str],
    ) -> str:
        app_client = self._get_app_client(app)
        return app_client.generate_vite_asset(path, **kwargs)

    def preload_vite_asset(
        self,
        path: str,
        app: str = DEFAULT_APP_NAME,
    ) -> str:
        app_client = self._get_app_client(app)
        return app_client.preload_vite_asset(path)

    def generate_vite_asset_url(
        self,
        path: str,
        app: str = DEFAULT_APP_NAME,
    ) -> str:
        app_client = self._get_app_client(app)
        return app_client.generate_vite_asset_url(path)

    def generate_vite_legacy_polyfills(
        self,
        app: str = DEFAULT_APP_NAME,
        **kwargs: Dict[str, str],
    ) -> str:
        app_client = self._get_app_client(app)
        return app_client.generate_vite_legacy_polyfills(**kwargs)

    def generate_vite_legacy_asset(
        self,
        path: str,
        app: str = DEFAULT_APP_NAME,
        **kwargs: Dict[str, str],
    ) -> str:
        app_client = self._get_app_client(app)
        return app_client.generate_vite_legacy_asset(path, **kwargs)

    def generate_vite_ws_client(
        self,
        app: str = DEFAULT_APP_NAME,
        **kwargs: Dict[str, str],
    ) -> str:
        app_client = self._get_app_client(app)
        return app_client.generate_vite_ws_client(**kwargs)

    def generate_vite_react_refresh_url(
        self,
        app: str = DEFAULT_APP_NAME,
        **kwargs: Dict[str, str],
    ) -> str:
        app_client = self._get_app_client(app)
        return app_client.generate_vite_react_refresh_url(**kwargs)
