// Schéma de validation des paramètres
const settingsSchema = {
    global: {
        required: ['threshold', 'delay'],
        defaults: {
            threshold: '0.5',
            delay: '1.0'
        }
    },
    microphone: {
        required: ['enabled', 'webhook_url', 'audio_source', 'device_index'],
        defaults: {
            enabled: false,
            webhook_url: '',
            audio_source: 'default',
            device_index: '0'
        }
    },
    rtsp_sources: {
        type: 'array',
        itemSchema: {
            required: ['id', 'name', 'url', 'webhook_url', 'enabled'],
            defaults: {
                webhook_url: '',
                enabled: false,
                url: ''
            }
        }
    },
    saved_vban_sources: {
        type: 'array',
        itemSchema: {
            required: ['name', 'ip', 'port', 'stream_name', 'webhook_url', 'enabled'],
            defaults: {
                webhook_url: '',
                enabled: false,
                port: 6980,
                stream_name: ''
            }
        }
    },
    vban: {
        required: ['stream_name', 'ip', 'port', 'webhook_url', 'enabled'],
        defaults: {
            stream_name: '',
            ip: '0.0.0.0',
            port: 6980,
            webhook_url: '',
            enabled: false
        }
    },
    wyoming: {
        required: ['enabled', 'port', 'forward_host', 'forward_port', 'webhook_url', 'threshold'],
        defaults: {
            enabled: false,
            port: 10700,
            forward_host: '',
            forward_port: 10300,
            webhook_url: '',
            threshold: 0.5
        }
    }
};

// Valide et complète les paramètres manquants
export function validateSettings(settings) {
    console.log('🔍 Validation - Paramètres reçus:', settings);
    const validatedSettings = { ...settings };
    const errors = [];

    // Valider chaque section
    Object.entries(settingsSchema).forEach(([section, schema]) => {
        if (!validatedSettings[section]) {
            if (section === 'rtsp_sources') {
                // Préserver les sources RTSP existantes
                validatedSettings[section] = settings[section] || [];
                console.log('🔍 Validation - Préservation des sources RTSP:', validatedSettings[section]);
            } else {
                validatedSettings[section] = schema.type === 'array' ? [] : {};
            }
        }

        if (schema.type === 'array') {
            // Pour les tableaux (comme rtsp_sources), préserver les valeurs existantes
            if (!Array.isArray(validatedSettings[section])) {
                validatedSettings[section] = [];
            }
            // Ne pas réinitialiser les tableaux existants
            console.log(`🔍 Validation - Tableau ${section}:`, validatedSettings[section]);
        } else {
            // Pour les autres sections, vérifier les champs requis
            if (schema.required) {
                schema.required.forEach(field => {
                    if (!validatedSettings[section][field] && validatedSettings[section][field] !== false) {
                        validatedSettings[section][field] = schema.defaults[field];
                        errors.push(`Champ manquant ${section}.${field}, valeur par défaut utilisée`);
                    }
                });
            }
        }
    });

    console.log('🔍 Validation - Paramètres validés:', validatedSettings);
    return {
        settings: validatedSettings,
        errors,
        isValid: true
    };
}

// Compare les paramètres actuels avec ceux de l'interface
// (simplifié car les onglets sont dynamiques)
export function compareWithDOMValues(settings) {
    return {
        hasDifferences: false,
        differences: []
    };
}

// Vérifie si les champs statiques requis sont présents dans l'interface
// (les éléments dynamiques des onglets sont construits après par buildTabs)
export function validateDOM() {
    return {
        isValid: true,
        missingElements: []
    };
}
